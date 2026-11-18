// The entry price of an open position, worked out of the fills, without a browser. The contract keeps no entry price,
// only the size of a position; the price is the size-weighted price of the fills that built the position as it stands.
// Where the fills at hand do not prove it, there is no price. app/lib/entrytrack.js reads the fills and keeps the result.
import { triggerLedger } from "./exam.js";

const abs = (n) => (n < 0n ? -n : n);
const sign = (n) => (n > 0n ? 1 : n < 0n ? -1 : 0);
const signed = (t) => (t.isBuy ? t.size : -t.size);

/**
 * The trades of one asset out of recorded events (`events`: [{name, args, block?}] in the order the chain wrote them): a
 * `Filled`, and the `TriggerFilled` that follows one a stop or a take carried out, are one trade between them. Null where
 * the record is not what the contract writes.
 */
export function tradesOf(events, asset) {
  try {
    return triggerLedger(events.filter((e) => e.name === "Filled" || e.name === "TriggerFilled")).trades.filter((t) => t.asset === Number(asset));
  } catch {
    return null;
  }
}

/** The running state of a position: its size (signed) and what was paid for it (price units times size units). */
export const FLAT = Object.freeze({ held: 0n, cost: 0 });

/**
 * The state after one fill ({isBuy, size, px}). More on the side held moves the average by size; part of the position
 * closed leaves the average where it was; a fill that carries the position through zero opens the other side at its own
 * price, for what is left; a fill on a flat account opens it.
 */
export function foldFill(state, fill) {
  const delta = signed(fill);
  const px = Number(fill.px);
  const held = state.held;
  if (held === 0n || sign(held) === sign(delta)) return { held: held + delta, cost: state.cost + px * Number(fill.size) };
  if (fill.size <= abs(held)) return { held: held + delta, cost: state.cost * (Number(abs(held) - fill.size) / Number(abs(held))) };
  const after = held + delta;
  return { held: after, cost: px * Number(abs(after)) };
}

export const foldFills = (state, fills) => fills.reduce(foldFill, state);

/** The average price of a state, or null where it holds nothing. */
export const entryOfState = (state) => (state.held === 0n ? null : state.cost / Number(abs(state.held)));

/** A state that holds `size` bought or sold at `entry`: what a kept record says. */
export const stateOf = (size, entry) => ({ held: BigInt(size), cost: entry * Number(abs(BigInt(size))) });

/**
 * The entry price of a position of `size` (signed, as the chain reports it) out of `trades` (oldest first, all of them
 * from some block to now). The trades are taken from the newest back, undoing each one on the position: the position
 * before a trade is known exactly, and where it was flat, or on the other side, that trade opened the position as it
 * stands (a trade through zero opens it for what is left of it). The trades from there on are the whole history of the
 * position. Where the trades run out before that, nothing proves the price, and the answer is null; so it is for a
 * position that is not open.
 */
export function entryFromFills(trades, size) {
  const open = BigInt(size);
  if (!trades || open === 0n) return null;
  let after = open;
  let from = -1;
  let first = null;
  for (let i = trades.length - 1; i >= 0; i -= 1) {
    const before = after - signed(trades[i]);
    if (before === 0n) {
      from = i;
      break;
    }
    if (sign(before) !== sign(open)) {
      from = i;
      first = { isBuy: open > 0n, size: abs(after), px: trades[i].px };
      break;
    }
    after = before;
  }
  if (from < 0) return null;
  const state = foldFills(FLAT, first ? [first, ...trades.slice(from + 1)] : trades.slice(from));
  return state.held === open ? entryOfState(state) : null;
}

/** `entryFromFills` of recorded events. */
export const entryPrice = (events, asset, size) => entryFromFills(tradesOf(events, asset), size);
