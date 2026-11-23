// A pool of exams, the user panel's partner and author cabinet (#/sim/<pool>/partner), built to mockup 05: the codes
// that name the connected wallet, what each has accrued and what waits, what it has received, and the Claim button.
// The tab is shown only to a wallet that is written in a code (revealPartnerTab). The pool has no list of its codes, so
// they are found in its `CodeSet` events, a window of blocks at a time (the node answers a range of 1 000 blocks);
// what was read is said on the page, and a button reads further. The words and the figures are app/lib/simpartner.js.
import * as chain from "../lib/chain.js";
import { esc, render, $, friendly } from "../lib/ui.js";
import { createConfirm, createPending } from "../lib/simconfirm.js";
import { SIM_POOL_ABI } from "../lib/simpool.js";
import { userHeaderHtml, footerHtml } from "../lib/simpanel.js";
import { cabinetRows, claimCall, partnerHtml, partnerNoteHtml, noCodeHtml, sendsTo } from "../lib/simpartner.js";
import { scanOf, finished, note, read, catchUp, confirmedEvents, currentCodes, revealPartnerTab, PAGE_CALLS, MORE_CALLS } from "./simcodes.js";
import { readFailure } from "../lib/simindex.js";
import { accountLabel, wireShell } from "./simshell.js";

const { ethers } = window;
// One claim (and the redraw after it) at a time, however the page is drawn again; and the press that arms a claim is the
// one that sends it only while the screen is the same.
const pending = createPending();
const confirm = createConfirm();
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

const READ_FURTHER = `<button type="button" class="so-btn" id="sq-more">Read further</button>`;

/** The cabinet, drawn into `page`. A read that fails is said on the page, with a way to try again; the windows the scan
 *  has read stay read. `opts.more`: windows to read further, both ways (null: the first reading); `opts.notice`: what to
 *  say in the status line once drawn, `{ ok, words }`. */
export async function simPartnerView(address, page, opts = {}) {
  const at = ethers.getAddress(address);
  const account = chain.currentAddress();
  const name = `Pool ${chain.short(at)}`;
  const shell = (inner) => `<div class="sp sp-user sp-partner">${userHeaderHtml({ address: at, account: accountLabel() })}${inner}${footerHtml()}</div>`;
  const statusLine = `<p id="sim-status" class="sp-status" role="status"></p>`;
  const state = { rows: [] };
  confirm.reset();

  // One click handler for the page, replaced when the page is drawn again: a second one would answer twice, and the
  // page's other handlers are left alone.
  page.__partnerClicks?.abort();
  const clicks = new AbortController();
  page.__partnerClicks = clicks;
  page.addEventListener("click", (e) => {
    if (pending.active) return;
    if (e.target.closest("#sq-more")) {
      simPartnerView(at, page, { more: MORE_CALLS });
      return;
    }
    if (e.target.closest("#sq-retry")) {
      simPartnerView(at, page, { more: opts.more ?? null });
      return;
    }
    const one = e.target.closest("button[data-claim]");
    if (one && !one.disabled) return press(one, state.rows.filter((r) => r.code === one.dataset.claim));
    const all = e.target.closest("#sq-claim-all");
    if (all && !all.disabled) press(all, state.rows.filter((r) => r.hasWaiting));
  }, { signal: clicks.signal });

  const say = (ok, words) => {
    const box = $("#sim-status", page);
    if (!box) return;
    box.className = `sp-status ${ok === null ? "" : ok ? "ok" : "err"}`;
    box.textContent = words;
  };

  /** A first press says what the claim will do and arms the button; the second press, with the same call, sends it. */
  function press(button, chosen) {
    let call;
    try {
      call = claimCall({ pool: at, rows: chosen, keccak256: ethers.keccak256 });
    } catch (err) {
      say(false, err.message);
      return;
    }
    const args = call.args[0];
    const got = confirm.press(call.data, () => args);
    if (got.action === "arm") {
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
      say(null, call.words);
      return;
    }
    send(got.run());
  }

  async function send(codes) {
    await pending.run(async () => {
      page.querySelectorAll("button").forEach((b) => (b.disabled = true));
      say(null, "Confirm in your wallet…");
      let notice;
      try {
        const signed = new ethers.Contract(at, SIM_POOL_ABI, chain.currentSigner());
        const receipt = await (await signed.claimShares(codes)).wait();
        chain.forgetLiveReads();
        if (!receipt || receipt.status !== 1) throw new Error("the claim reverted");
        notice = { ok: true, words: `Sent (transaction ${receipt.hash}).` };
      } catch (err) {
        notice = { ok: false, words: friendly(err) };
      }
      // The cabinet is read again from the chain; a failure of that is said on the page, with the claim's own answer.
      await simPartnerView(at, page, { more: 0, notice });
    });
  }

  const finish = () => {
    if (opts.notice) say(opts.notice.ok, opts.notice.words);
  };

  if (!account) {
    render(page, shell(noCodeHtml(false)));
    wireShell(page);
    return;
  }
  render(page, shell(`<section class="sp-msg"><h2>Partner cabinet</h2><p>Looking for your codes in the pool's events…</p></section>${statusLine}`));
  wireShell(page);
  try {
    const st = await scanOf(at);
    await catchUp(at, st);
    // The codes are searched from the pool's creation forward, the sends from the newest block back; `more` reads both
    // further. After a claim (`more` is zero) nothing more is read but the new blocks.
    if (opts.more == null) await read(at, st, PAGE_CALLS, PAGE_CALLS);
    else await read(at, st, opts.more, opts.more);
    // Held to the chain: the events say where a code was, `codeOf` what it is now.
    const codes = await currentCodes(at, account, st);
    state.rows = cabinetRows({ account, codes: await readCodes(at, codes) });
    const left = finished(st) ? "" : `<p class="so-why">${esc(note(st))} ${READ_FURTHER}</p>`;
    if (state.rows.length === 0) {
      render(page, shell(`${noCodeHtml(true)}${left}${statusLine}`));
      wireShell(page);
      finish();
      return;
    }
    // A send the index named is shown once its receipt confirms it (simcodes.js, confirmedEvents).
    const { events, left: leftOut } = await confirmedEvents(at, st, account);
    await timesOf(st, sendsTo(events, account));
    const timed = sendsTo(events, account, st.times);
    const unchecked = leftOut ? ` ${leftOut} send${leftOut === 1 ? "" : "s"} the index named could not be checked against the chain yet and ${leftOut === 1 ? "is" : "are"} not shown.` : "";
    render(page, shell(`${partnerNoteHtml(chain.short(account), name)}${partnerHtml({ rows: state.rows, sends: timed, scanNote: `${note(st)}${unchecked}`, poolName: name.toUpperCase() })}`));
    wireShell(page);
    await revealPartnerTab(page, at, { current: true });
    if (!finished(st)) $("#sq-scan", page)?.insertAdjacentHTML("beforeend", ` ${READ_FURTHER}`);
    finish();
  } catch (err) {
    // Said on the page that is showing, not on one that is gone; what the scan has read stays read.
    render(page, shell(`<section class="sp-msg"><h2>Partner cabinet</h2><p>Could not read the pool: ${esc(readFailure(err))}</p>`
      + `<p><button type="button" class="so-btn" id="sq-retry">Try again</button></p></section>${statusLine}`));
    wireShell(page);
    finish();
  }
}
