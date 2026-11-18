// Where the entry price of the open position comes from: the fills read from the exam's logs, proven against the size
// the chain reports, and kept in the browser so a later visit does not read again. The rules about fills are
// app/lib/entry.js; what is read and what is kept are handed in, so this file has no browser and no chain of its own.
//
//   const entries = createEntryTracker({ address, read, store, onChange });
//   entries.observe({ asset, size, block, fillAt, cash });   // the market on screen: its size now (signed bigint), the block it
//                                                            // was read at, and the exam's `lastFillAt` and `cash` read with it
//   entries.entry(asset, size);                  // the price in price units, or null where none is proven for what was observed last
//   entries.feed({ events, from, to }, {...});   // events already read (the journal's): they may prove it too
//   entries.stop();                              // the page was left
//
// A kept proof stands only while the exam shows no fill since it was made: the size, `lastFillAt` (the time of the exam's
// latest fill of any market, set by every fill of $10 or more) and `cash` (moved by every fill, a small one too) are all
// as they were. Else the fills since are read and added, or the proof is thrown out.
//
// Nothing is read on a timer. A reading is made when a market is seen with a state no proof stands for, once for that state:
// from the last block a kept proof knows (or from `READ_BLOCKS` back), to the block the size was read at, never more than
// `READ_BLOCKS` blocks (the same reading as one press of the journal's button). Where it does not prove the price, or the node
// refuses, there is no price and nothing is said; the next reading is made when the state changes.
import { stateOf, foldFills, entryOfState, tradesOf, entryFromFills } from "./entry.js";

/** The blocks one reading covers: the journal's 20 calls of 50 blocks (JOURNAL_CALLS and JOURNAL_WINDOW in app/views/exam.js). */
export const READ_BLOCKS = 1000;
const PREFIX = "exam-entry-v1:";

/** The browser's storage as `{get, set, remove}` that never throws: a browser without storage keeps nothing. */
export function browserStore(storage) {
  const use = (fn) => {
    try {
      return fn(storage ?? globalThis.localStorage);
    } catch {
      return null;
    }
  };
  return {
    get: (key) => use((s) => s.getItem(key)),
    set: (key, value) => { use((s) => s.setItem(key, value)); },
    remove: (key) => { use((s) => s.removeItem(key)); },
  };
}

const keyOf = (address, asset) => `${PREFIX}${String(address).toLowerCase()}:${Number(asset)}`;

const WHOLE = /^-?[0-9]+$/;

/** A kept record, or null where it is not one: {size, fillAt, cash (texts of whole numbers), entry (a positive number), block}. */
function parse(text) {
  try {
    const r = JSON.parse(text);
    if (typeof r?.size !== "string" || !/^-?[1-9][0-9]*$/.test(r.size) || !(r.entry > 0) || !Number.isFinite(r.entry) || !Number.isInteger(r.block) || r.block < 0
      || typeof r.fillAt !== "string" || !WHOLE.test(r.fillAt) || typeof r.cash !== "string" || !WHOLE.test(r.cash)) return null;
    return { size: BigInt(r.size), entry: r.entry, block: r.block, fillAt: BigInt(r.fillAt), cash: BigInt(r.cash) };
  } catch {
    return null;
  }
}

/**
 * The entry price proven by `events` (decoded, with `block`, covering every block from `from` to `to`) for a position of
 * `size`, given the record kept for it (or null). With a record whose block is inside what was read, the fills after it are
 * added to what it says; otherwise the fills are undone from the newest back. Null where neither comes to the size.
 */
export function prove({ events, from, asset, size, record }) {
  const wanted = BigInt(size);
  if (record && record.block + 1 >= from) {
    const trades = tradesOf(events.filter((e) => e.block > record.block), asset);
    if (trades) {
      const state = foldFills(stateOf(record.size, record.entry), trades);
      if (state.held === wanted) return entryOfState(state);
    }
  }
  return entryFromFills(tradesOf(events, asset), wanted);
}

export function createEntryTracker({ address, read, store, onChange = () => {} }) {
  const known = new Map();   // asset -> {size, entry, block, fillAt, cash}: a proof
  const tried = new Map();   // asset -> the state (size, fillAt, cash) a reading was made for
  const seen = new Map();    // asset -> the state observed last
  let busy = false;
  let running = null;
  let waiting = null;
  let dead = false;

  const keep = (asset) => {
    const k = known.get(asset);
    if (k) {
      store.set(keyOf(address, asset), JSON.stringify({ size: k.size.toString(), entry: k.entry, block: k.block, fillAt: k.fillAt.toString(), cash: k.cash.toString() }));
    }
  };
  const drop = (asset) => {
    known.delete(asset);
    store.remove(keyOf(address, asset));
  };
  const recordOf = (asset) => {
    if (!known.has(asset)) {
      const kept = parse(store.get(keyOf(address, asset)));
      if (kept) known.set(asset, kept);
    }
    const k = known.get(asset);
    return k ?? null;
  };
  const stateKey = ({ size, fillAt, cash }) => `${size}:${fillAt}:${cash}`;
  const same = (k, o) => k.size === o.size && k.fillAt === o.fillAt && k.cash === o.cash;
  const settle = (asset, o, block, entry) => {
    if (entry === null) {
      const had = known.has(asset);
      drop(asset);
      return had;
    }
    known.set(asset, { size: o.size, entry, block, fillAt: o.fillAt, cash: o.cash });
    keep(asset);
    return true;
  };

  async function work(req) {
    const { asset, size, block } = req;
    // With a kept proof only what came after it is read (at most one reading back); without one, one reading back.
    const record = recordOf(asset);
    const back = Math.max(0, block - READ_BLOCKS + 1);
    const from = record ? Math.max(back, Math.min(record.block + 1, block)) : back;
    let entry = null;
    try {
      const events = await read(from, block);
      if (dead) return;
      entry = prove({ events, from, asset, size, record });
    } catch {
      // the node refused or did not answer: no price, and the page says nothing
    }
    if (dead) return;
    tried.set(asset, stateKey(req));
    if (settle(asset, req, block, entry)) onChange();
  }

  async function run(req) {
    busy = true;
    let next = req;
    while (next && !dead) {
      running = next;
      await work(next);
      next = waiting;
      waiting = null;
      if (next && ((known.get(next.asset) && same(known.get(next.asset), next)) || tried.get(next.asset) === stateKey(next))) next = null;
    }
    busy = false;
    running = null;
  }

  return {
    observe({ asset, size, block, fillAt, cash }) {
      if (dead) return;
      const now = BigInt(size);
      if (now === 0n) {
        tried.delete(asset);
        seen.delete(asset);
        if (known.has(asset) || store.get(keyOf(address, asset)) !== null) {
          drop(asset);
          onChange();
        }
        return;
      }
      const req = { asset, size: now, block: Number(block), fillAt: BigInt(fillAt), cash: BigInt(cash) };
      seen.set(asset, stateKey(req));
      const k = recordOf(asset);
      if (k && same(k, req)) return;
      if (tried.get(asset) === stateKey(req)) return;
      if (!busy) run(req);
      else if (!(running.asset === asset && stateKey(running) === stateKey(req))) waiting = req;
    },
    entry(asset, size) {
      const k = known.get(asset) ?? recordOf(asset);
      return k && k.size === BigInt(size) && seen.get(asset) === stateKey(k) ? k.entry : null;
    },
    feed({ events, from, to }, { asset, size, block, fillAt, cash }) {
      if (dead || BigInt(size) === 0n || Number(to) < Number(block)) return;
      const o = { size: BigInt(size), fillAt: BigInt(fillAt), cash: BigInt(cash) };
      seen.set(asset, stateKey(o));
      const k = recordOf(asset);
      if (k && same(k, o)) return;
      const entry = prove({ events, from, asset, size, record: k });
      if (entry !== null && settle(asset, o, Number(block), entry)) {
        tried.set(asset, stateKey(o));
        onChange();
      }
    },
    stop() {
      dead = true;
    },
  };
}
