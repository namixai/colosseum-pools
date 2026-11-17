// A pool of exams, the user panel's partner and author cabinet (#/sim/<pool>/partner), built to mockup 05: the codes
// that name the connected wallet, what each has accrued and what waits, what it has received, and the Claim button.
// The tab is shown only to a wallet that is written in a code (revealPartnerTab). The pool has no list of its codes, so
// they are found in its `CodeSet` events, a window of blocks at a time (the node answers a range of 1 000 blocks);
// what was read is said on the page, and a button reads further. The words and the figures are app/lib/simpartner.js.
import * as chain from "../lib/chain.js";
import { esc, render, $, friendly } from "../lib/ui.js";
import { SIM_POOL_ABI } from "../lib/simpool.js";
import { userHeaderHtml, footerHtml } from "../lib/simpanel.js";
import { codesNamingAccount, cabinetRows, claimCall, partnerHtml, partnerNoteHtml, noCodeHtml, sendsTo } from "../lib/simpartner.js";
import { scanOf, complete, note, read, catchUp, eventList, revealPartnerTab, PAGE_CALLS, MORE_CALLS } from "./simcodes.js";
import { accountLabel, wireShell } from "./simshell.js";

const { ethers } = window;
/** What the cabinet reads of the wallet's codes: `codeOf` and `shareDue` of each. */
async function readCodes(address, codes) {
  const pool = new ethers.Contract(address, SIM_POOL_ABI, chain.readProvider);
  return chain.readAll(codes, async (code) => {
    const [c, due] = await Promise.all([pool.codeOf(code), pool.shareDue(code)]);
    return { code, c, due };
  });
}

async function timesOf(st, sends) {
  const blocks = [...new Set(sends.map((s) => s.block))].filter((n) => !st.times.has(n)).slice(0, 40);
  await Promise.all(blocks.map(async (n) => {
    try {
      const b = await chain.readProvider.getBlock(n);
      if (b) st.times.set(n, b.timestamp);
    } catch {
      // a send without its time says its block
    }
  }));
}

export async function simPartnerView(address, page, { more = null } = {}) {
  const at = ethers.getAddress(address);
  const account = chain.currentAddress();
  const name = `Pool ${chain.short(at)}`;
  const shell = (inner) => `<div class="sp sp-user sp-partner">${userHeaderHtml({ address: at, account: accountLabel() })}${inner}${footerHtml()}</div>`;
  if (!account) {
    render(page, shell(noCodeHtml(false)));
    wireShell(page);
    return;
  }
  render(page, shell(`<section class="sp-msg"><h2>Partner cabinet</h2><p>Looking for your codes in the pool's events…</p></section>`));
  wireShell(page);
  const st = await scanOf(at);
  await catchUp(at, st);
  // The codes are searched from the pool's creation forward, the sends from the newest block back; `more` reads both
  // further. After a claim (`more` is zero) nothing more is read but the new blocks.
  if (more === null) await read(at, st, PAGE_CALLS, PAGE_CALLS);
  else await read(at, st, more, more);
  const codes = codesNamingAccount(eventList(st), account);
  const rows = cabinetRows({ account, codes: await readCodes(at, codes) });
  if (rows.length === 0) {
    render(page, shell(`${noCodeHtml(true)}${complete(st) ? "" : `<p class="so-why">${esc(note(st))} <button type="button" class="so-btn" id="sq-more">Read further</button></p>`}`));
    wireShell(page);
    const again = $("#sq-more", page);
    if (again) again.addEventListener("click", () => simPartnerView(at, page, { more: MORE_CALLS }).catch((err) => { again.textContent = friendly(err); }));
    return;
  }
  const sends = sendsTo(eventList(st), account);
  await timesOf(st, sends);
  const timed = sendsTo(eventList(st), account, st.times);
  render(page, shell(`${partnerNoteHtml(chain.short(account), name)}${partnerHtml({ rows, sends: timed, scanNote: note(st), poolName: name.toUpperCase() })}`));
  wireShell(page);
  await revealPartnerTab(page, at, { current: true });
  if (!complete(st)) {
    const box = $("#sq-scan", page);
    if (box) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "so-btn";
      button.textContent = "Read further";
      button.addEventListener("click", () => simPartnerView(at, page, { more: MORE_CALLS }).catch((err) => { box.textContent = friendly(err); }));
      box.append(" ", button);
    }
  }

  const status = $("#sim-status", page);
  // A first press says what the claim will do and arms the button; the second press sends it.
  let armed = null;
  const press = async (button, chosen, key) => {
    let call;
    try {
      call = claimCall({ pool: at, rows: chosen, keccak256: ethers.keccak256 });
    } catch (err) {
      status.className = "sp-status err";
      status.textContent = err.message;
      return;
    }
    if (armed !== key) {
      armed = key;
      page.querySelectorAll("button[data-armed]").forEach((b) => { b.removeAttribute("data-armed"); b.textContent = b.dataset.was; });
      page.querySelectorAll(".sq-hint[data-was]").forEach((h) => { h.textContent = h.dataset.was; h.removeAttribute("data-was"); });
      button.dataset.was = button.textContent;
      button.dataset.armed = "1";
      button.textContent = "Press again to sign";
      // What the claim will do is said beside the button, and again in the status line.
      const hint = button.parentElement.querySelector(".sq-hint");
      if (hint) {
        hint.dataset.was = hint.textContent;
        hint.textContent = call.words;
      }
      status.className = "sp-status";
      status.textContent = call.words;
      return;
    }
    armed = null;
    page.querySelectorAll("button.sq-claim").forEach((b) => (b.disabled = true));
    status.className = "sp-status busy";
    status.textContent = "Confirm in your wallet…";
    let words;
    let ok = false;
    try {
      const signed = new ethers.Contract(at, SIM_POOL_ABI, chain.currentSigner());
      const receipt = await (await signed.claimShares(call.args[0])).wait();
      chain.forgetLiveReads();
      if (!receipt || receipt.status !== 1) throw new Error("the claim reverted");
      words = `Sent (transaction ${receipt.hash}).`;
      ok = true;
    } catch (err) {
      words = friendly(err);
    }
    try {
      await simPartnerView(at, page, { more: 0 });
    } catch (err) {
      words = `${words} The page could not be read again (${friendly(err)}). Reload it to see the figures as the chain has them.`;
    }
    const after = $("#sim-status", page);
    if (after) {
      after.className = `sp-status ${ok ? "ok" : "err"}`;
      after.textContent = words;
    }
  };
  // One handler for the page: the page node is the same when the cabinet is drawn again, and a second listener would send twice.
  page.onclick = (e) => {
    const one = e.target.closest("button[data-claim]");
    if (one && !one.disabled) {
      press(one, rows.filter((r) => r.code === one.dataset.claim), one.dataset.claim);
      return;
    }
    const all = e.target.closest("#sq-claim-all");
    if (all && !all.disabled) press(all, rows.filter((r) => r.hasWaiting), "all");
  };
}
