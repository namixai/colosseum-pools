// The reading of a pool's stop events, apart from the node: the windows, the batches, the cursors and the state that is
// kept for a pool. No browser globals, so node's test runner loads it (app/tests/simoperator.test.mjs); the view
// (app/views/simstopscan.js) gives it `read(from, to)`, which asks the node for the logs of a range of blocks.
//
// A cursor moves only past a batch that was read whole, so a batch that fails leaves no hole: what was found before is kept
// and returned with the failure, and the same range is asked for again at the next press. One reading of a pool at a time.

/** The node answers a range of 1 000 blocks, and an open node refuses logs asked for too often: a few windows at once. */
export const WINDOW = 1000;
export const BATCH = 3;

export const newScan = ({ floor, latest }) => ({ floor, latest, head: latest, back: latest, events: new Map() });

/** The logs of a window, as stop events; a log without the stop's id (a node's odd answer) is skipped, not guessed at. */
export function addLogs(st, logs) {
  for (const l of logs ?? []) {
    if (!l || !Array.isArray(l.topics) || l.topics.length < 2) continue;
    st.events.set(`${l.blockNumber}:${l.index}`, { name: "PayoutStopped", args: { id: Number(BigInt(l.topics[1])) }, block: l.blockNumber });
  }
}

/** The ranges to read: up to `forward` windows from the cursor `latest` up to `head`, and `backward` from `back` down to `floor`. */
export function planRanges(st, { forward, backward }) {
  const ahead = [];
  for (let from = st.latest + 1; ahead.length < forward && from <= st.head; from += WINDOW) ahead.push([from, Math.min(from + WINDOW - 1, st.head)]);
  const behind = [];
  for (let to = st.back; behind.length < backward && to >= st.floor; to -= WINDOW) behind.push([Math.max(to - WINDOW + 1, st.floor), to]);
  return { ahead, behind };
}

async function readBatches(st, ranges, read, advance) {
  for (let i = 0; i < ranges.length; i += BATCH) {
    const batch = ranges.slice(i, i + BATCH);
    const answers = await Promise.all(batch.map(([from, to]) => read(from, to)));
    // The batch was read whole: its logs are kept and the cursor goes past it.
    answers.forEach((logs) => addLogs(st, logs));
    advance(batch);
  }
}

/**
 * Reads the planned ranges. Never throws for a failed batch: it returns `{ failure }` (the error, or null), with what was
 * found before kept in `st` and the failed range still ahead of its cursor.
 */
export async function scanStops(st, { read, forward, backward }) {
  const { ahead, behind } = planRanges(st, { forward, backward });
  try {
    await readBatches(st, ahead, read, (batch) => { st.latest = batch[batch.length - 1][1]; });
    await readBatches(st, behind, read, (batch) => { st.back = batch[batch.length - 1][0] - 1; });
    return { failure: null };
  } catch (err) {
    return { failure: err };
  }
}

export const isDone = (st) => st.back < st.floor && st.latest >= st.head;

/** Runs `job` for `key` after any job already running for it: one reading of a pool at a time. */
export function serial(locks, key, job) {
  const last = locks.get(key) ?? Promise.resolve();
  const next = last.then(job, job);
  locks.set(key, next.catch(() => {}));
  return next;
}
