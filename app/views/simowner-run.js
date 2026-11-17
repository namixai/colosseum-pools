// What the owner's sections of the control panel share: the pool's contract with every call they read or send, and one
// runner for a call that goes through the wallet (one at a time, with the words before and after).
import * as chain from "../lib/chain.js";
import { $, friendly, refusalName, esc } from "../lib/ui.js";
import { SIM_POOL_ABI } from "../lib/simpool.js";
import { SIM_LINES_ABI, SIM_OWNER_ABI } from "../lib/simlines.js";
import { SIM_OWNER_CALLS_ABI, ownerRefusal } from "../lib/simowner.js";

const { ethers } = window;

/** Everything the owner's sections read from or send to a pool. */
export const OWNER_ABI = [...SIM_POOL_ABI, ...SIM_LINES_ABI, ...SIM_OWNER_ABI, ...SIM_OWNER_CALLS_ABI];

/** The pool for reading, or, with `signed`, for sending through the connected wallet. */
export const poolAt = (at, signed = false) => new ethers.Contract(at, OWNER_ABI, signed ? chain.currentSigner() : chain.readProvider);

let busy = false;

/**
 * Runs `task(signed, iface, say)` once at a time: the buttons of the page are held while it runs, `say` writes a step
 * into the status line, and what the task returns (its words) or the contract's refusal in words ends up in the status.
 * `refresh()` then draws the section again from the chain, whether the call went through or was refused; a page that
 * cannot be read again keeps the old one and says so.
 */
export async function runOwner(page, { at, refresh }, statusId, task) {
  if (busy) return;
  busy = true;
  const buttons = [...page.querySelectorAll("button")];
  const was = buttons.map((b) => b.disabled);
  buttons.forEach((b) => { b.disabled = true; });
  const status = $(`#${statusId}`, page);
  const say = (text, kind = "busy") => { status.className = `sp-status ${kind}`; status.textContent = text; };
  say("Working…");
  let words;
  let ok = false;
  const iface = new ethers.Interface(OWNER_ABI);
  try {
    if (!chain.currentSigner()) await chain.connect();
    words = await task(poolAt(at, true), iface, say);
    ok = true;
  } catch (err) {
    words = ownerRefusal(refusalName(err, iface), err?.revert?.args ? [...err.revert.args] : []) || friendly(err);
  }
  try {
    await refresh();
  } catch (err) {
    buttons.forEach((b, i) => { if (b.isConnected) b.disabled = was[i]; });
    words = `${words} The page could not be read again (${friendly(err)}). Reload it to see the pool as the chain has it.`;
  }
  const after = $(`#${statusId}`, page);
  if (after) {
    after.className = `sp-status ${ok ? "ok" : "err"}`;
    after.textContent = words;
  }
  busy = false;
}

/** Waits for a sent transaction and says so if it reverted; returns the receipt. */
export async function mined(sent, what) {
  const receipt = await (await sent).wait();
  chain.forgetLiveReads();
  if (!receipt || receipt.status !== 1) throw new Error(`${what} reverted`);
  return receipt;
}

/** The events of a receipt that this pool wrote, parsed: `{ name, args }`. */
export function eventsOf(receipt, at, iface) {
  const out = [];
  for (const log of receipt.logs || []) {
    if (String(log.address).toLowerCase() !== String(at).toLowerCase()) continue;
    try {
      const parsed = iface.parseLog(log);
      if (parsed) out.push({ name: parsed.name, args: parsed.args });
    } catch {
      // an event the pool's ABI here does not declare
    }
  }
  return out;
}

/** A section that could not be read: its box, with the reason and nothing to press but a reload. */
export function failedBoxHtml(id, title, err) {
  return `<section class="sp-box" id="${esc(id)}"><div class="sp-bh"><b>${esc(title)}</b></div>`
    + `<p class="so-failed" role="status">This section could not be read from the chain: ${esc(friendly(err))} Reload the page to try again.</p></section>`;
}
