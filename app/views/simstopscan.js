// The stops of a pool, found in its `PayoutStopped` events. A stop stays open to a lift for 14 days, and the pool's
// `head()` passes a stopped payout, so the payout's number says nothing about whether the stop is still open: the events
// name every stop whatever its number, and `payout(id)` then says where each stands. The node answers a range of 1 000
// blocks, so the last 14 days (a day or so of blocks more) are read from the newest block back, a window at a time, a few
// at first and more at each press of "Read earlier stops"; the page says which blocks were read. The reading is kept for
// the pool, so a redraw after a lift reads only the blocks written since.
import * as chain from "../lib/chain.js";
import { SIM_POOL_ABI, STOP_RESERVE, firstBlockAt } from "../lib/simpool.js";
import { stopIdsFromEvents, stopScanNote } from "../lib/simoperator.js";

const { ethers } = window;
const WINDOW = 1000;
export const FIRST_CALLS = 20;
export const MORE_CALLS = 30;
const CATCH_UP_CALLS = 8;
/** A day of margin below the 14 days: a stop made final is not needed, one just past its 14 days still can be made final. */
const MARGIN = 24 * 3600;

const scans = new Map();

async function scanOf(address, now) {
  const key = address.toLowerCase();
  if (scans.has(key)) return { st: scans.get(key), fresh: false };
  const iface = new ethers.Interface(SIM_POOL_ABI);
  const latest = await chain.readProvider.getBlockNumber();
  const timeOf = async (n) => (await chain.readProvider.getBlock(n)).timestamp;
  const floor = await firstBlockAt(Number(now) - STOP_RESERVE - MARGIN, { low: chain.deployBlock(), high: latest, timeOf }) ?? chain.deployBlock();
  const st = { topic: iface.getEvent("PayoutStopped").topicHash, floor, latest, head: latest, back: latest, events: new Map() };
  scans.set(key, st);
  return { st, fresh: true };
}

async function window_(address, st, from, to) {
  const logs = await chain.readProvider.getLogs({ address, topics: [st.topic], fromBlock: from, toBlock: to });
  for (const l of logs) st.events.set(`${l.blockNumber}:${l.index}`, { name: "PayoutStopped", args: { id: Number(BigInt(l.topics[1])) }, block: l.blockNumber });
}

/**
 * The ids of the stops found so far, and how far the reading has come. `calls`: windows to read further back (null: the
 * first reading of this pool, a few windows); the blocks written since the last reading are always read.
 */
export async function lookForStops(address, now, calls = null) {
  const { st, fresh } = await scanOf(address, now);
  const head = await chain.readProvider.getBlockNumber();
  st.head = Math.max(st.head, head);
  for (let i = 0; i < CATCH_UP_CALLS && st.latest < st.head; i++) {
    const end = Math.min(st.latest + WINDOW, st.head);
    await window_(address, st, st.latest + 1, end);
    st.latest = end;
  }
  const windows = calls ?? (fresh ? FIRST_CALLS : 0);
  for (let i = 0; i < windows && st.back >= st.floor; i++) {
    const begin = Math.max(st.back - WINDOW + 1, st.floor);
    await window_(address, st, begin, st.back);
    st.back = begin - 1;
  }
  const done = st.back < st.floor && st.latest >= st.head;
  return { ids: stopIdsFromEvents([...st.events.values()]), done, note: stopScanNote({ floor: st.floor, back: st.back, latest: st.latest, head: st.head, done }) };
}
