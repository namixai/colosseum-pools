// What the partner's cabinet finds in a pool's events: its codes (`CodeSet`) and the sends to a wallet (`SharePaid`). The
// pool has no list of its codes, so they are read from its logs, a window of blocks at a time (the node answers a range of
// 1 000 blocks), from the pool's creation forward and, for the sends, from the newest block back. What was read is said,
// and a button reads further. Kept apart from the cabinet's page: the user panel's menu asks it too, to show the Partner
// tab to a wallet that is written in a code.
//
// The pool's index on the host (app/lib/simindex.js) is asked first: it has the events from the pool's creation to a block
// near the newest, and the page reads only the blocks after it. What it names is a candidate: a code is held to `codeOf`
// (`currentCodes`), a send to its transaction's receipt (`confirmSends`). Without the index the page reads the chain
// itself, as before, and says why.
import * as chain from "../lib/chain.js";
import { CONFIG } from "../config.js";
import { SIM_POOL_ABI, MIN_TERM, firstBlockAt, readLogs, normalizeEvent } from "../lib/simpool.js";
import { codesNamingAccount, confirmedCodes, partnerHref } from "../lib/simpartner.js";
import { askIndex, indexBase, sourceNote, readShare, sendInReceipt } from "../lib/simindex.js";

const { ethers } = window;
/** The node's limit on a range of blocks (measured: 1 000), and the windows a page reads at once. */
const WINDOW = 1000;
const REVEAL_CALLS = 10;
export const PAGE_CALLS = 20;
export const MORE_CALLS = 30;
const CATCH_UP_CALLS = 8;

const scans = new Map();

/**
 * The CodeSet and SharePaid events of a pool, read from its creation forward and from the newest block back, in windows.
 * One entry for each pool: the page that comes next goes on from where this one stopped. `lockedUntil - MIN_TERM` is the
 * time the pool was created, which a halving over block times turns into the block to start from.
 */
export async function scanOf(address) {
  const key = address.toLowerCase();
  if (scans.has(key)) return scans.get(key);
  const iface = new ethers.Interface(SIM_POOL_ABI);
  const pool = new ethers.Contract(address, SIM_POOL_ABI, chain.readProvider);
  const [index, latest] = await Promise.all([
    askIndex({ base: indexBase(CONFIG), pool: address, chainId: CONFIG.chainId, fetch: (...a) => window.fetch(...a) }),
    chain.readProvider.getBlockNumber(),
  ]);
  let start;
  if (index.ok) start = Math.min(index.created, latest);
  else {
    const created = Number(await pool.lockedUntil()) - MIN_TERM;
    const timeOf = async (n) => (await chain.readProvider.getBlock(n)).timestamp;
    start = await firstBlockAt(created, { low: chain.deployBlock(), high: latest, timeOf }) ?? latest;
  }
  const st = { iface, topics: [iface.getEvent("CodeSet").topicHash, iface.getEvent("SharePaid").topicHash], start, latest,
    head: latest, fwd: start, back: latest, events: new Map(), times: new Map(), checked: new Map(), source: index };
  if (index.ok) {
    // The index's blocks are read: the page goes on from the block after its last one.
    for (const e of index.events) if (e.name === "CodeSet" || e.name === "SharePaid") st.events.set(`${e.block}:${e.index}`, e);
    st.fwd = Math.max(start, Math.min(index.scannedTo, latest) + 1);
  }
  scans.set(key, st);
  return st;
}

export const complete = (st) => st.fwd > st.back;

export function note(st) {
  const source = sourceNote(st.source);
  const lead = source ? `${source} ` : "";
  if (finished(st)) return `${lead}All of the pool's events were read.`;
  const newest = st.latest < st.head ? ` Blocks after ${st.latest} (the newest is ${st.head}) are not read.` : "";
  const read = (st.fwd - st.start) + (st.latest - st.back);
  const share = readShare({ from: st.start, to: st.head, read });
  return `${lead}Read blocks ${st.start}–${st.fwd - 1} from the pool's creation and ${st.back + 1}–${st.latest} from the newest `
    + `(${share}% of the pool's blocks).${newest} A code set, or a send made, in the blocks between is not shown until you read further.`;
}

/**
 * The pool's events with the sends to `account` the chain does not confirm left out. A send the page read from the chain
 * itself is the chain's; one the index named stays only when its transaction's receipt has the pool's `SharePaid` with the
 * same fields. A receipt that cannot be read leaves the send out this time, and it is asked for again next time. Returns
 * the events and how many sends were left out.
 */
export async function confirmedEvents(address, st, account) {
  const topic = st.iface.getEvent("SharePaid").topicHash;
  const mine = (e) => e.fromIndex && e.name === "SharePaid" && String(e.args.to).toLowerCase() === String(account).toLowerCase();
  const all = eventList(st);
  const ask = [...new Set(all.filter((e) => mine(e) && !st.checked.has(e.tx)).map((e) => e.tx))].slice(0, 40);
  await Promise.all(ask.map(async (tx) => {
    try {
      const receipt = await chain.readProvider.getTransactionReceipt(tx);
      if (receipt) st.checked.set(tx, receipt);
    } catch {
      // not read this time: the send is left out and asked for again
    }
  }));
  const events = all.filter((e) => !mine(e) || (st.checked.has(e.tx) && sendInReceipt(st.checked.get(e.tx), e, { pool: address, topic })));
  return { events, left: all.length - events.length };
}

async function readWindow(address, st, from, to) {
  const { logs } = await readLogs({ from, to, window: WINDOW, maxCalls: 1,
    getLogs: (x, y) => chain.readProvider.getLogs({ address, topics: [st.topics], fromBlock: x, toBlock: y }) });
  for (const l of logs) {
    const parsed = st.iface.parseLog(l);
    if (!parsed) continue;
    st.events.set(`${l.blockNumber}:${l.index}`, normalizeEvent({ eventName: parsed.name, args: parsed.args, blockNumber: l.blockNumber,
      transactionHash: l.transactionHash, index: l.index }));
  }
}

/** Reads `forward` windows from the creation and `backward` from the newest block, within what is left unread. */
export async function read(address, st, forward, backward) {
  for (let i = 0; i < forward && !complete(st); i++) {
    const end = Math.min(st.fwd + WINDOW - 1, st.back);
    await readWindow(address, st, st.fwd, end);
    st.fwd = end + 1;
  }
  for (let i = 0; i < backward && !complete(st); i++) {
    const begin = Math.max(st.back - WINDOW + 1, st.fwd);
    await readWindow(address, st, begin, st.back);
    st.back = begin - 1;
  }
}

/** The blocks written since the page last read, a few windows of them: a send made a moment ago is there at once. */
export async function catchUp(address, st) {
  const head = await chain.readProvider.getBlockNumber();
  st.head = Math.max(st.head, head);
  for (let i = 0; i < CATCH_UP_CALLS && st.latest < st.head; i++) {
    const end = Math.min(st.latest + WINDOW, st.head);
    await readWindow(address, st, st.latest + 1, end);
    st.latest = end;
  }
}

export const eventList = (st) => [...st.events.values()];

/** Whether every block has been read: the whole history, and the blocks written since. */
export const finished = (st) => complete(st) && st.latest >= st.head;

/**
 * The wallet's codes as the chain has them now. The events only say where to look (a code may have been set again to
 * another address since); each candidate is held to the pool's `codeOf`, which has the last word.
 */
export async function currentCodes(address, account, st) {
  const candidates = codesNamingAccount(eventList(st), account);
  if (candidates.length === 0) return [];
  const pool = new ethers.Contract(address, SIM_POOL_ABI, chain.readProvider);
  const reads = await chain.readAll(candidates, async (code) => ({ code, c: await pool.codeOf(code) }));
  return confirmedCodes(reads, account);
}

/** Puts the Partner tab in the user panel's menu, when the chain names the connected wallet in a code of this pool; the
 *  page `current` marks it as the page being shown. Nothing is added for any other wallet, and a failed or unfinished look
 *  adds nothing and is not remembered: the next page asks again, from where the reading stopped. */
export async function revealPartnerTab(page, pool, { current = false } = {}) {
  const account = chain.currentAddress();
  const nav = page.querySelector(".sp-top .sp-nav");
  if (!account || !nav) return false;
  const at = ethers.getAddress(pool);
  let found = false;
  try {
    const st = await scanOf(at);
    await catchUp(at, st);
    await read(at, st, REVEAL_CALLS, 0);
    found = (await currentCodes(at, account, st)).length > 0;
  } catch {
    found = false;
  }
  if (!found) return false;
  if (!nav.querySelector("[data-partner]")) {
    const link = document.createElement("a");
    link.href = partnerHref(at);
    link.dataset.partner = "1";
    link.textContent = "Partner";
    nav.append(link);
  }
  if (current) {
    nav.querySelectorAll("a.on").forEach((a) => { a.classList.remove("on"); a.removeAttribute("aria-current"); });
    const mine = nav.querySelector("[data-partner]");
    mine.classList.add("on");
    mine.setAttribute("aria-current", "page");
    const route = page.querySelector(".sp-strip .sp-route");
    if (route) route.textContent = partnerHref(at);
  }
  return true;
}

