// The stops of a pool, found in its `PayoutStopped` events. A stop stays open to a lift for 14 days, and the pool's
// `head()` passes a stopped payout, so the payout's number says nothing about whether the stop is still open: the events
// name every stop whatever its number, and `payout(id)` then says where each stands. The last 14 days (a day or so of
// blocks more) are read from the newest block back, a few windows at first and more at each press of "Read earlier
// stops"; the page says which blocks were read. The reading is kept for the pool, so a redraw after a lift reads only the
// blocks written since. The windows, batches and cursors are app/lib/stopscan.js; a batch that fails keeps what was found.
import * as chain from "../lib/chain.js";
import { SIM_POOL_ABI, STOP_RESERVE, firstBlockAt } from "../lib/simpool.js";
import { stopIdsFromEvents, stopScanNote } from "../lib/simoperator.js";
import { newScan, scanStops, isDone, serial } from "../lib/stopscan.js";

const { ethers } = window;
export const FIRST_CALLS = 20;
export const MORE_CALLS = 30;
const CATCH_UP_CALLS = 8;
/** A day of margin below the 14 days: a stop made final is not needed, one just past its 14 days still can be made final. */
const MARGIN = 24 * 3600;

const scans = new Map();
const locks = new Map();

async function scanOf(address, now) {
  const key = address.toLowerCase();
  if (scans.has(key)) return { st: scans.get(key), fresh: false };
  const iface = new ethers.Interface(SIM_POOL_ABI);
  const latest = await chain.readProvider.getBlockNumber();
  const timeOf = async (n) => (await chain.readProvider.getBlock(n)).timestamp;
  const floor = await firstBlockAt(Number(now) - STOP_RESERVE - MARGIN, { low: chain.deployBlock(), high: latest, timeOf }) ?? chain.deployBlock();
  const st = newScan({ floor, latest });
  st.topic = iface.getEvent("PayoutStopped").topicHash;
  scans.set(key, st);
  return { st, fresh: true };
}

/**
 * The ids of the stops found so far, and how far the reading has come. `calls`: windows to read further back (null: the
 * first reading of this pool, a few windows); the blocks written since the last reading are always read. A batch that
 * cannot be read does not lose the stops found before it: they are returned with `done: false` and the failure in the
 * note, and the same blocks are asked for at the next press. Readings of one pool go one at a time.
 */
export function lookForStops(address, now, calls = null) {
  return serial(locks, address.toLowerCase(), async () => {
    const { st, fresh } = await scanOf(address, now);
    let failure = null;
    try {
      st.head = Math.max(st.head, await chain.readProvider.getBlockNumber());
    } catch (err) {
      failure = err;
    }
    if (!failure) {
      const read = (from, to) => chain.readProvider.getLogs({ address, topics: [st.topic], fromBlock: from, toBlock: to });
      ({ failure } = await scanStops(st, { read, forward: CATCH_UP_CALLS, backward: calls ?? (fresh ? FIRST_CALLS : 0) }));
    }
    const done = isDone(st) && !failure;
    const note = stopScanNote({ floor: st.floor, back: st.back, latest: st.latest, head: st.head, done });
    return { ids: stopIdsFromEvents([...st.events.values()]), done, failed: Boolean(failure),
      note: failure ? `${note} The last batch of blocks could not be read; press the button to ask for it again.` : note };
  });
}
