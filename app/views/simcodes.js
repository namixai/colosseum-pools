// What the partner's cabinet finds in a pool's events: its codes (`CodeSet`) and the sends to a wallet (`SharePaid`). The
// pool has no list of its codes, so they are read from its logs, a window of blocks at a time (the node answers a range of
// 1 000 blocks), from the pool's creation forward and, for the sends, from the newest block back. What was read is said,
// and a button reads further. Kept apart from the cabinet's page: the user panel's menu asks it too, to show the Partner
// tab to a wallet that is written in a code.
import * as chain from "../lib/chain.js";
import { SIM_POOL_ABI, MIN_TERM, firstBlockAt, readLogs, normalizeEvent } from "../lib/simpool.js";
import { codesNamingAccount, confirmedCodes, partnerHref } from "../lib/simpartner.js";

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
  const [lockedUntil, latest] = await Promise.all([pool.lockedUntil(), chain.readProvider.getBlockNumber()]);
  const created = Number(lockedUntil) - MIN_TERM;
  const timeOf = async (n) => (await chain.readProvider.getBlock(n)).timestamp;
  const start = await firstBlockAt(created, { low: chain.deployBlock(), high: latest, timeOf }) ?? latest;
  const st = { iface, topics: [iface.getEvent("CodeSet").topicHash, iface.getEvent("SharePaid").topicHash], start, latest,
    head: latest, fwd: start, back: latest, events: new Map(), times: new Map() };
  scans.set(key, st);
  return st;
}

export const complete = (st) => st.fwd > st.back;

export function note(st) {
  if (finished(st)) return "All of the pool's events were read.";
  const newest = st.latest < st.head ? ` Blocks after ${st.latest} (the newest is ${st.head}) are not read.` : "";
  return `Read blocks ${st.start}–${st.fwd - 1} from the pool's creation and ${st.back + 1}–${st.latest} from the newest.${newest} `
    + "A code set, or a send made, in the blocks between is not shown until you read further.";
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

