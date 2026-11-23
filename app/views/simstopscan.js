// The stops of a pool, found in its `PayoutStopped` events. A stop stays open to a lift for 14 days, and the pool's
// `head()` passes a stopped payout, so the payout's number says nothing about whether the stop is still open: the events
// name every stop whatever its number, and `payout(id)` then says where each stands. The last 14 days (a day or so of
// blocks more) are read from the newest block back, a few windows at first and more at each press of "Read earlier
// stops"; the page says which blocks were read. The reading is kept for the pool, so a redraw after a lift reads only the
// blocks written since. The windows, batches and cursors are app/lib/stopscan.js; a batch that fails keeps what was found.
//
// The pool's index on the host (app/lib/simindex.js) is asked first: it names every stop from the pool's creation to a
// block near the newest, and the page reads the blocks after it. A stop it names is only an id: `payout(id)` and
// `stopReasonOf(id)` are read from the pool before it is shown (simoperator.js, readOperatorPool). Without the index the
// page reads the chain itself and says why; a read that fails is said in words, never as the node's raw error.
import * as chain from "../lib/chain.js";
import { CONFIG } from "../config.js";
import { SIM_POOL_ABI, STOP_RESERVE, firstBlockAt } from "../lib/simpool.js";
import { stopIdsFromEvents, stopScanNote } from "../lib/simoperator.js";
import { newScan, scanStops, isDone, serial } from "../lib/stopscan.js";
import { askIndex, indexBase, sourceNote, readFailure } from "../lib/simindex.js";

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
  const [index, latest] = await Promise.all([
    askIndex({ base: indexBase(CONFIG), pool: address, chainId: CONFIG.chainId, fetch: (...a) => window.fetch(...a) }),
    chain.readProvider.getBlockNumber(),
  ]);
  let st;
  if (index.ok) {
    // Every stop since the pool's creation, to the index's last block; the page reads on from there.
    const floor = Math.min(index.created, latest);
    st = newScan({ floor, latest: Math.max(floor - 1, Math.min(index.scannedTo, latest)) });
    st.head = latest;
    st.back = floor - 1;
    for (const e of index.events) if (e.name === "PayoutStopped") st.events.set(`${e.block}:${e.index}`, e);
  } else {
    const timeOf = async (n) => (await chain.readProvider.getBlock(n)).timestamp;
    const floor = await firstBlockAt(Number(now) - STOP_RESERVE - MARGIN, { low: chain.deployBlock(), high: latest, timeOf }) ?? chain.deployBlock();
    st = newScan({ floor, latest });
  }
  st.topic = iface.getEvent("PayoutStopped").topicHash;
  st.source = index;
  scans.set(key, st);
  return { st, fresh: !index.ok };
}

/**
 * The ids of the stops found so far, and how far the reading has come. `calls`: windows to read further back (null: the
 * first reading of this pool, a few windows); the blocks written since the last reading are always read. A batch that
 * cannot be read does not lose the stops found before it: they are returned with `done: false` and the failure in the
 * note, and the same blocks are asked for at the next press. Readings of one pool go one at a time.
 */
export function lookForStops(address, now, calls = null) {
  return serial(locks, address.toLowerCase(), async () => {
    let scan;
    try {
      scan = await scanOf(address, now);
    } catch (err) {
      // Not even the newest block or the 14 days' first block could be read: said in words, and the button asks again.
      return { ids: [], done: false, failed: true, note: `Stops could not be looked for yet: ${readFailure(err)}.` };
    }
    const { st, fresh } = scan;
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
    const scanned = stopScanNote({ floor: st.floor, back: st.back, latest: st.latest, head: st.head, done, sinceCreation: Boolean(st.source?.ok) });
    const note = `${sourceNote(st.source)} ${scanned}`.trim();
    return { ids: stopIdsFromEvents([...st.events.values()]), done, failed: Boolean(failure),
      note: failure ? `${note} The last batch of blocks could not be read (${readFailure(failure)}).` : note };
  });
}
