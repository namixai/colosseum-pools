// The control panel's "Codes & shares" (mockup 02): the pool's codes with what each has accrued and waiting, and the
// form that sets one, or one for two. The pool has no list of its codes: they are named by the pool's index on the host
// (app/lib/simindex.js) up to a block near the newest, found in the `CodeSet` events of the blocks after it, and in the
// texts this browser has set or looked up; every one is read from `codeOf` and `shareDue` before it is listed. Without
// the index the events are read from the chain itself, from the newest block back, a stretch at a time, and the page says
// why and which blocks were read. Any text typed is checked against `codeOf`. Nothing here moves money: a code is written
// into the pool, and the shares accrue at the owner's withdrawals.
import * as chain from "../lib/chain.js";
import { CONFIG } from "../config.js";
import { $, friendly } from "../lib/ui.js";
import { codeHash } from "../lib/simpool.js";
import { askIndex, indexBase, readFailure } from "../lib/simindex.js";
import { codeRead, codeRow, codePlan, checkWords, scanNote, codesHtml, codeLink } from "../lib/simowner.js";
import { poolAt, runOwner, mined, failedBoxHtml } from "./simowner-run.js";

const { ethers } = window;
/** The node answers a range of 1 000 blocks (app/views/simcodes.js), and a press reads this many windows each way. */
const WINDOW = 1000;
const CALLS = 20;

/** What each pool's scan has found so far: the blocks read (one run) and the hashes of the codes in them. */
const scans = new Map();
/** What the page last drew, for the buttons of its rows and its form. */
let shown = { at: "", rows: [] };

// The texts of the codes this browser knows are kept per pool in its own storage; the page works without it.
const storeKey = (at) => `upools.owner.codes.${String(at).toLowerCase()}`;
function loadTexts(at) {
  try {
    const parsed = JSON.parse(localStorage.getItem(storeKey(at)) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}
function rememberText(at, hash, text) {
  try {
    const texts = loadTexts(at);
    texts[hash] = text;
    localStorage.setItem(storeKey(at), JSON.stringify(texts));
  } catch {
    // no storage: the code stays in the pool, only this browser forgets its text
  }
}

/**
 * Reads up to CALLS windows of `[from, to]`, oldest first (`up`) or newest first, into `s.found`; a window that fails ends
 * the reading with what was found kept. Returns the blocks read, as `[low, high]` or null, and the failure.
 */
async function readWindows(at, topic, pool, s, from, to, up) {
  let low = null;
  let high = null;
  for (let i = 0; i < CALLS && from <= to; i++) {
    const a = up ? from : Math.max(from, to - WINDOW + 1);
    const b = up ? Math.min(to, from + WINDOW - 1) : to;
    let logs;
    try {
      logs = await chain.readProvider.getLogs({ address: at, topics: [topic], fromBlock: a, toBlock: b });
    } catch (err) {
      return { low, high, failure: err };
    }
    for (const l of logs) {
      const parsed = pool.interface.parseLog(l);
      if (parsed) s.found.set(parsed.args.code, Math.max(s.found.get(parsed.args.code) ?? 0, l.blockNumber));
    }
    low = low === null ? a : Math.min(low, a);
    high = high === null ? b : Math.max(high, b);
    if (up) from = b + 1;
    else to = a - 1;
  }
  return { low, high, failure: null };
}

async function scanEvents(at, pool, more) {
  const latest = await chain.readProvider.getBlockNumber();
  const topic = pool.interface.getEvent("CodeSet").topicHash;
  let s = scans.get(at);
  if (!s) {
    const source = await askIndex({ base: indexBase(CONFIG), pool: at, chainId: CONFIG.chainId, fetch: (...a) => window.fetch(...a) });
    s = { from: latest + 1, top: latest, found: new Map(), source, floor: chain.deployBlock() };
    if (source.ok) {
      s.from = Math.min(source.created, latest);
      s.top = Math.max(s.from - 1, Math.min(source.scannedTo, latest));
      s.floor = s.from;
      for (const e of source.events) if (e.name === "CodeSet") s.found.set(e.args.code, e.block);
    }
    scans.set(at, s);
  }
  let failed = null;
  // The blocks written since the last reading, oldest first.
  const newer = await readWindows(at, topic, pool, s, s.top + 1, latest, true);
  if (newer.high !== null) s.top = newer.high;
  failed = newer.failure;
  // Further back, newest first: at the first reading without the index, and at each press of "Load earlier blocks".
  const first = !s.source.ok && s.from > latest;
  if (!failed && (first || more) && s.from > s.floor) {
    const older = await readWindows(at, topic, pool, s, s.floor, s.from - 1, false);
    if (older.low !== null) s.from = older.low;
    failed = older.failure;
  }
  return { latest, from: s.from, to: s.top, found: s.found, source: s.source, failed: failed ? readFailure(failed) : "" };
}

/** The codes of the pool: found in its events and in this browser's texts, each read from `codeOf` and `shareDue`. */
export async function readCodes(at, { more = false } = {}) {
  try {
    const pool = poolAt(at);
    const [scan, sharesDue] = await Promise.all([scanEvents(at, pool, more), pool.sharesDue()]);
    const texts = loadTexts(at);
    const hashes = [...scan.found.entries()].sort((a, b) => b[1] - a[1]).map(([h]) => h);
    for (const h of Object.keys(texts)) if (!hashes.includes(h)) hashes.push(h);
    const rows = await chain.readAll(hashes, async (h) => {
      const [read, due] = await Promise.all([pool.codeOf(h), pool.shareDue(h)]);
      return codeRow(codeRead(h, read), due, texts[h] || "");
    });
    const real = rows.filter((r) => r.partner && !/^0x0{40}$/i.test(r.partner));
    const known = real.filter((r) => !scan.found.has(r.code)).length;
    return { rows: real, sharesDue, more: !scan.source.ok,
      scan: scanNote({ from: scan.from, to: scan.to, latest: scan.latest, known, source: scan.source, failed: scan.failed }) };
  } catch (err) {
    return { error: err };
  }
}

/** The section's markup from what `readCodes` answered. */
export function codesSection(data) {
  if (data.error) return failedBoxHtml("codes", "Codes & shares", data.error);
  return codesHtml(data);
}

const field = (form, name) => form.elements[name].value;
const percentText = (bps) => String(bps / 100);

/** Wires the section: the form's live check, the rows' buttons, the call. `ctx.at` is the pool. */
export function wireCodes(page, ctx, data) {
  shown = { at: ctx.at, rows: data.error ? [] : data.rows };
  const box = $("#codes", page);
  if (!box || data.error) return;
  const status = () => $("#so-codes-status", page);
  const form = $("#so-code-form", page);
  const check = $("#so-check", page);
  const planBox = $("#so-plan", page);
  const send = $("#so-code-send", page);
  let existing = null;
  let readFor = "";
  let latestPlan = null;
  let timer = null;

  const keccak = (hex) => ethers.keccak256(hex);
  const replan = () => {
    const text = field(form, "text");
    latestPlan = codePlan({ text, partner: field(form, "partner"), share: field(form, "share"), author: field(form, "author"),
      authorShare: field(form, "authorShare"), existing, keccak256: keccak });
    check.textContent = text ? checkWords(existing, text) : "";
    const filled = [...form.elements].some((el) => el.name && el.value);
    planBox.className = `so-plan${latestPlan.ok ? "" : " bad"}`;
    planBox.textContent = !filled ? "" : latestPlan.ok ? latestPlan.words : latestPlan.errors.join(" ");
    send.disabled = !latestPlan.ok;
    send.textContent = latestPlan.ok && latestPlan.mode === "raise" ? "Raise the share" : "Set the code";
  };
  // The typed text is looked up in the pool (`codeOf`) before anything is planned from it.
  const lookup = async () => {
    const text = field(form, "text");
    if (!text) { existing = null; readFor = ""; replan(); return; }
    let hash;
    try { hash = codeHash(text, ethers.keccak256); } catch { existing = null; replan(); return; }
    if (hash === readFor) {
      if (existing && existing.partner && !/^0x0{40}$/i.test(existing.partner)) rememberText(ctx.at, hash, text);
      replan();
      return;
    }
    readFor = hash;
    try {
      const read = codeRead(hash, await poolAt(ctx.at).codeOf(hash));
      if (readFor !== hash) return;
      existing = read;
      if (read.partner && !/^0x0{40}$/i.test(read.partner)) rememberText(ctx.at, hash, text);
    } catch (err) {
      if (readFor !== hash) return;
      existing = null;
      check.textContent = `The pool could not be asked about this code (${friendly(err)}).`;
    }
    replan();
  };
  form.addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(e.target.name === "text" ? lookup : replan, e.target.name === "text" ? 300 : 0);
  });

  $("#so-new-code", page).addEventListener("click", () => {
    // A new code starts from an empty form, whatever was being edited before.
    form.reset();
    existing = null;
    readFor = "";
    replan();
    form.hidden = false;
    form.elements.text.focus();
  });
  $("#so-code-cancel", page).addEventListener("click", () => { form.hidden = true; });
  $("#so-more-codes", page).addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    await repaint(page, ctx, { more: true });
  });

  box.addEventListener("click", async (e) => {
    const button = e.target.closest("button[data-act]");
    if (!button) return;
    const row = shown.rows.find((r) => r.code === button.dataset.code);
    if (!row) return;
    if (button.dataset.act === "copy") {
      const link = codeLink(ctx.at, row.text, `${location.origin}${location.pathname}`);
      try {
        await navigator.clipboard.writeText(link);
        status().className = "sp-status ok";
        status().textContent = `Link copied: ${link}. Give it to the group's traders; it opens the pool's trader page with the code filled in.`;
      } catch {
        status().className = "sp-status";
        status().textContent = `Copy this link by hand: ${link}`;
      }
      return;
    }
    form.hidden = false;
    form.elements.text.value = row.text || "";
    form.elements.partner.value = row.partner;
    form.elements.share.value = percentText(row.bps);
    form.elements.author.value = row.author || "";
    form.elements.authorShare.value = row.author ? percentText(row.authorBps) : "";
    existing = row;
    readFor = row.text ? "" : row.code;
    if (row.text) await lookup();
    else {
      replan();
      check.textContent = `Type this code's text to change it: the pool keeps only its hash ${row.code}. ${checkWords(row, "")}`;
    }
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
    (row.text ? form.elements.share : form.elements.text).focus();
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!latestPlan || !latestPlan.ok) return;
    const plan = latestPlan;
    const text = field(form, "text");
    runOwner(page, { at: ctx.at, refresh: () => repaint(page, ctx, {}) }, "so-codes-status", async (signed, iface, say) => {
      say(`${plan.words} Sign it in your wallet.`);
      const receipt = await mined(signed[plan.call.fn](...plan.call.args), plan.call.fn);
      rememberText(ctx.at, plan.code, text);
      const s = scans.get(ctx.at);
      if (s) s.found.set(plan.code, receipt.blockNumber);
      return `${plan.mode === "raise" ? "Share raised" : "Code set"}: "${text}" (transaction ${receipt.hash}). ${plan.mode === "raise" ? "The new share counts from your next withdrawal." : "Give your group the link: it is in this code's row."}`;
    });
  });
}

/** Reads the section again and draws it in place; the form is closed. */
async function repaint(page, ctx, { more = false } = {}) {
  const data = await readCodes(ctx.at, { more });
  const old = $("#codes", page);
  if (!old) return;
  const keep = $("#so-codes-status", page);
  const text = keep ? keep.textContent : "";
  const cls = keep ? keep.className : "";
  const holder = document.createElement("div");
  holder.innerHTML = codesSection(data);
  old.replaceWith(holder.firstElementChild);
  wireCodes(page, ctx, data);
  const status = $("#so-codes-status", page);
  if (status && text) { status.textContent = text; status.className = cls; }
}
