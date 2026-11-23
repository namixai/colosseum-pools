// The sim pools' event index on the host (gateway/simindex.py), as the panels use it: the owner's "Codes & shares", the
// operator's stops and the partner's cabinet. A pool keeps no list of its codes or its stops, and its history is millions
// of blocks, too many to read from a browser; the host reads them once and answers `GET /v1/simindex/pool?pool=0x…`
// with the pool's events and the last block it read.
//
// The page does not take the index's word. What it answers is a list of candidates: each code is read from the pool
// (`codeOf`, `shareDue`), each stop from `payout(id)`, each send from its transaction's receipt, and only what the chain
// says is shown. The blocks after the index's last one are read from the chain by the page itself. An index that does not
// answer, or answers something this file cannot read, is said on the page, which then reads the chain itself, a window at
// a time, with what it has read in view and a button to read further. No browser globals: node's test runner loads it
// (app/tests/simindex.test.mjs); the views give it `fetch`.
import { friendly } from "./ui.js";

/** How long the page waits for the index before it reads the chain itself. */
export const INDEX_TIMEOUT_MS = 6000;

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const WORD = /^0x[0-9a-fA-F]{64}$/;
const NAMES = new Set(["CodeSet", "SharePaid", "PayoutStopped", "StopLifted", "StopFinal"]);
const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
const block = (n) => Number.isSafeInteger(n) && n >= 0;

/** The index's address for a pool, on the gateway's host. Over https, or http to this machine only, as the orders go. */
export function indexUrl(base, pool) {
  const url = new URL(base);
  const local = url.hostname === "127.0.0.1" || url.hostname === "localhost" || url.hostname === "[::1]";
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
    throw new Error(`the index must be reached over https, not ${url.protocol}//${url.host}`);
  }
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/v1/simindex/pool`;
  url.search = `?pool=${encodeURIComponent(pool)}`;
  url.hash = "";
  return url.toString();
}

/** One event of the index as the panels read events (app/lib/simpool.js, normalizeEvent), or null for one of another
 *  shape: an entry that cannot be read is left out, never guessed at. */
export function eventOf(e) {
  if (!e || typeof e !== "object" || !NAMES.has(e.name) || !block(e.block) || !block(e.index) || !WORD.test(e.tx ?? "")) return null;
  const base = { name: e.name, block: e.block, index: e.index, tx: e.tx, fromIndex: true };
  if (e.name === "CodeSet") {
    if (!WORD.test(e.code ?? "") || !ADDRESS.test(e.partner ?? "") || !ADDRESS.test(e.author ?? "")) return null;
    if (!Number.isInteger(e.bps) || !Number.isInteger(e.authorBps) || e.bps < 0 || e.bps > 10000 || e.authorBps < 0 || e.authorBps > 10000) return null;
    return { ...base, args: { code: e.code, partner: e.partner, bps: e.bps, author: e.author, authorBps: e.authorBps } };
  }
  if (e.name === "SharePaid") {
    if (!WORD.test(e.code ?? "") || !ADDRESS.test(e.to ?? "") || !/^\d{1,78}$/.test(String(e.amount)) || typeof e.sent !== "boolean") return null;
    return { ...base, args: { code: e.code, to: e.to, amount: BigInt(e.amount), sent: e.sent } };
  }
  if (!block(e.id)) return null;
  if (e.name === "PayoutStopped") {
    if (!ADDRESS.test(e.trader ?? "")) return null;
    return { ...base, args: { id: e.id, trader: e.trader } };
  }
  return { ...base, args: { id: e.id } };
}

/**
 * The index's answer for `pool`, checked: `{ ok: true, created, scannedTo, events, dropped }`, or `{ ok: false, why }`
 * when it is for another pool or chain, or not the shape the index answers. `dropped` counts the entries left out.
 */
export function checkAnswer(body, { pool, chainId }) {
  if (!body || typeof body !== "object" || body.status !== "ok") return { ok: false, why: "it answered something this page cannot read" };
  if (!same(body.pool, pool)) return { ok: false, why: "it answered for another pool" };
  if (Number(body.chain_id) !== Number(chainId)) return { ok: false, why: `it reads chain ${body.chain_id}, not this site's ${chainId}` };
  if (!block(body.created) || !block(body.scanned_to) || !Array.isArray(body.events)) return { ok: false, why: "it answered something this page cannot read" };
  const events = [];
  let dropped = 0;
  for (const raw of body.events) {
    const e = eventOf(raw);
    if (e && e.block <= body.scanned_to) events.push(e);
    else dropped += 1;
  }
  return { ok: true, created: body.created, scannedTo: body.scanned_to, events, dropped };
}

/** Asks the index for `pool`. Never throws: an index that does not answer in `timeoutMs`, answers an error or something
 *  that does not check out is `{ ok: false, why }` in words for the page. */
export async function askIndex({ base, pool, chainId, fetch, timeoutMs = INDEX_TIMEOUT_MS }) {
  let url;
  try {
    url = indexUrl(base, pool);
  } catch (err) {
    return { ok: false, why: err.message };
  }
  const ctl = typeof AbortController === "function" ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
  try {
    const res = await fetch(url, { method: "GET", signal: ctl?.signal, headers: { Accept: "application/json" } });
    const body = await res.json().catch(() => null);
    if (res.status === 404 && body?.code === "unknown_pool") return { ok: false, why: "it does not know this pool yet" };
    if (!res.ok) return { ok: false, why: `it answered HTTP ${res.status}` };
    return checkAnswer(body, { pool, chainId });
  } catch (err) {
    return { ok: false, why: err?.name === "AbortError" ? `it did not answer in ${Math.round(timeoutMs / 1000)} seconds` : "it could not be reached" };
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** What the page says of where its list comes from, before the list. */
export function sourceNote(source) {
  if (!source) return "";
  if (source.ok) {
    return `Found by the pool's index on the host up to block ${source.scannedTo}, the blocks after it read from the chain by this page; `
      + "every row is read from the pool itself before it is shown.";
  }
  return `The pool's index on the host is not available (${source.why}), so this page reads the pool's events from the chain `
    + "itself, a window of blocks at a time. That is slow for a pool that has run for weeks; what has been read is said below, "
    + "and the button reads further.";
}

/** How much of `[from, to]` has been read, as a whole percent, for a reading that goes on press by press. */
export function readShare({ from, to, read }) {
  const all = to - from + 1;
  if (!(all > 0)) return 100;
  return Math.max(0, Math.min(100, Math.floor((read / all) * 100)));
}

/**
 * A failed read in words for the page. A public node that refuses logs answers in ways ethers cannot name: Hyperliquid's
 * testnet node answers "rate limited" with `"id": null`, which ethers reports as "could not coalesce error" (23 Nov 2026),
 * and others a bad or missing response. That is said as what it is, the node not answering this read, with the way on.
 * Anything else is `friendly`'s words.
 */
export function readFailure(err) {
  const text = `${err?.code ?? ""} ${err?.shortMessage ?? ""} ${err?.message ?? ""}`;
  if (/coalesce|missing response|bad response|SERVER_ERROR|UNKNOWN_ERROR|TIMEOUT|timeout|Failed to fetch|NetworkError|block range|too many|exceed/i.test(text)) {
    return "the chain node did not answer this read (a public node limits the logs it serves); nothing found so far is lost, "
      + "press the button to ask again";
  }
  return friendly(err);
}

/**
 * Whether a send the index named is in its transaction's receipt as the pool wrote it: a `SharePaid` log of `pool` with
 * the same code, address, amount and `sent`. `receipt` is ethers' (logs with `address`, `topics`, `data`); `topic` is
 * the event's topic hash.
 */
export function sendInReceipt(receipt, ev, { pool, topic }) {
  if (!receipt || receipt.status === 0 || !Array.isArray(receipt.logs)) return false;
  const to = `0x${String(ev.args.to).slice(2).toLowerCase().padStart(64, "0")}`;
  const data = `0x${BigInt(ev.args.amount).toString(16).padStart(64, "0")}${(ev.args.sent ? 1n : 0n).toString(16).padStart(64, "0")}`;
  return receipt.logs.some((l) => same(l.address, pool) && Array.isArray(l.topics) && l.topics.length === 3
    && same(l.topics[0], topic) && same(l.topics[1], ev.args.code) && same(l.topics[2], to) && same(l.data, data));
}

/** Where the index answers: the site's `simIndex` when it names one, else the pool gateway's host. */
export const indexBase = (config) => config.simIndex || config.gateway;
