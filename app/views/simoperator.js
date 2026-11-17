// A pool of exams, the control panel's operator section (#/manage/<pool>/operator), built to mockup 04. It is a page of the
// management panel, apart from the owner's page and from the user panel: the wallet's roles are read from the chain (the
// pool's `approver()`, the factory's `checker()` and `closer()`), a wallet with none sees one sentence, and each role is
// drawn only the controls its contract call allows. Before a call is sent the page says in words what it will do. The
// words, the queue's states and the calls are app/lib/simoperator.js and app/lib/operator.js; this file reads and wires.
import * as chain from "../lib/chain.js";
import { render, $, friendly, refusalName, ZERO_ADDRESS } from "../lib/ui.js";
import { createConfirm, createPending } from "../lib/simconfirm.js";
import { lookForStops, MORE_CALLS } from "./simstopscan.js";
import { SIM_POOL_ABI, SIM_POOL_STOP_RESERVE_ABI, SIM_POOL_APPROVAL_ABI } from "../lib/simpool.js";
import { SIM_LINES_ABI } from "../lib/simlines.js";
import { OPERATOR_ABI } from "../lib/operator.js";
import { footerHtml } from "../lib/simpanel.js";
import {
  SIM_OPERATOR_ABI, SIM_OPERATOR_FACTORY_ABI, PAYOUT_WINDOW, payoutRange, mergeAbis, operatorRoles, operatorModel, operatorHeaderHtml,
  operatorHtml, messageHtml, selectionCall, approveAnywayCall, HOLD_WARNING, stopFromForm, stopWords, liftWords,
  finalizeWords, cancelWords, operatorRefusal, approveWords, STOP_FORM,
} from "../lib/simoperator.js";
import { accountLabel, wireShell } from "./simshell.js";

const { ethers } = window;
// `payout(id)` is named by three of the libraries with different fields: the widest comes first.
const ABI = mergeAbis(SIM_POOL_APPROVAL_ABI, SIM_POOL_STOP_RESERVE_ABI, SIM_OPERATOR_ABI, OPERATOR_ABI, SIM_LINES_ABI, SIM_POOL_ABI);
// One submission at a time for the whole page, however it is drawn again (a redraw enables its buttons afresh).
const pending = createPending();
// How many payouts the queue reads: each "Read later payouts" press adds a window.
const reading = new Map();
// Windows of stops' events to read further back at the next drawing (set by "Read earlier stops").
const moreStops = new Map();

/** The pool's approver, or nobody for a pool that does not answer `approver()`. */
async function approverOf(pool) {
  try {
    return await pool.approver();
  } catch (err) {
    if (err?.code === "CALL_EXCEPTION") return ZERO_ADDRESS;
    throw err;
  }
}

/** What the operator's page reads of the pool, for the roles the wallet has. */
export async function readOperatorPool(address, roles, limit = PAYOUT_WINDOW, stopCalls = null) {
  const pool = new ethers.Contract(address, ABI, chain.readProvider);
  const now = await chain.blockTime();
  const out = { now };
  const queue = roles.includes("approver") || roles.includes("checker");
  if (queue) {
    const [count, head, perDay, room, nextPayAt, menu, tiers] = await Promise.all([
      pool.payoutCount(), pool.head(), pool.payoutsPerDay(), pool.payRoom(), pool.nextPayAt(), pool.menu(), pool.tiers(),
    ]);
    // From the first payout not settled (and a way below it, for the stops still inside their 14 days), oldest first.
    const range = payoutRange({ count, head, limit });
    // A payout the node does not answer for is kept as a hole, and the page says it was not read: it is never dropped silently.
    const payouts = await chain.readAll(range.ids, (id) => pool.payout(id).catch(() => null));
    // The stops are found in the pool's events, whatever their number; each is then read by its id.
    let extraIds = [];
    let stopScan = null;
    if (roles.includes("checker")) {
      try {
        const found = await lookForStops(address, now, stopCalls);
        stopScan = { done: found.done, note: found.note };
        const inWindow = new Set(range.ids);
        extraIds = found.ids.filter((id) => !inWindow.has(id));
      } catch (err) {
        // The queue and the stops of its window still draw; the page says the search failed, and the button tries again.
        stopScan = { done: false, note: `Stops could not be looked for in the events (${friendly(err)}).` };
      }
    }
    const extraPayouts = await chain.readAll(extraIds, (id) => pool.payout(id).catch(() => null));
    // A stop named by an event whose payout was not read stays in `extra`, as not read; a payout read and not a stop is left out.
    const extra = extraIds.map((id, i) => ({ id, payout: extraPayouts[i] })).filter((x) => !x.payout || [1, 2, 5].includes(Number(x.payout.state)));
    const live = [...payouts, ...extra.map((x) => x.payout)].filter((p) => p && [1, 2, 5].includes(Number(p.state)));
    const accounts = [...new Set(live.map((p) => p.account.toLowerCase()))];
    const lines = await chain.readAll(accounts, async (a) => (await pool.accountOf(a)).line);
    const stoppedIds = [...range.ids.filter((id, i) => payouts[i] && [2, 5].includes(Number(payouts[i].state))),
      ...extra.filter((x) => x.payout && [2, 5].includes(Number(x.payout.state))).map((x) => x.id)];
    const why = await chain.readAll(stoppedIds, (id) => pool.stopReasonOf(id));
    Object.assign(out, { payouts, extra, stopScan, firstId: range.first, unread: range.unread, payoutsPerDay: perDay, payRoom: room, nextPayAt, menu, tiers,
      accountLines: new Map(accounts.map((a, i) => [a, Number(lines[i])])),
      reasons: new Map(stoppedIds.map((id, i) => [id, Number(why[i])])) });
  } else {
    Object.assign(out, { payouts: [], firstId: 0, unread: 0, payoutsPerDay: 0, payRoom: 0, nextPayAt: 0, menu: [], tiers: [] });
  }
  if (roles.includes("checker") || roles.includes("closer")) {
    const [requested, readyAt] = await Promise.all([pool.requested(), pool.readyAt()]);
    Object.assign(out, { requested, readyAt });
  }
  return out;
}

export async function simOperatorView(address, page) {
  const at = ethers.getAddress(address);
  const account = chain.currentAddress();
  const pool = new ethers.Contract(at, ABI, chain.readProvider);
  const factory = new ethers.Contract(await pool.factory(), SIM_OPERATOR_FACTORY_ABI, chain.readProvider);
  const [approver, checker, closer] = await Promise.all([approverOf(pool), factory.checker(), factory.closer()]);
  const who = operatorRoles({ account, approver, checker, closer });
  const name = `Pool ${chain.short(at)}`;
  const header = operatorHeaderHtml({ address: at, account: accountLabel(), state: who.kind, roles: who.roles });
  if (who.kind !== "operator") {
    // Nothing of the pool is read for a wallet with no role: the panel says what it is, and that is all.
    render(page, `<div class="sp sp-manage sp-op">${header}${messageHtml(who.kind)}${footerHtml()}</div>`);
    wireShell(page);
    return;
  }
  const stopCalls = moreStops.get(at) ?? null;
  moreStops.delete(at);
  const reads = await readOperatorPool(at, who.roles, reading.get(at) || PAYOUT_WINDOW, stopCalls);
  const model = operatorModel({ pool: at, roles: who.roles, reads: { ...reads, approver }, keccak256: ethers.keccak256 });
  render(page, `<div class="sp sp-manage sp-op">${header}${operatorHtml(model, { name })}${footerHtml()}</div>`);
  wireShell(page);
  wire(page, at, model);
}

function wire(page, at, model) {
  const status = $("#sim-status", page);
  const iface = new ethers.Interface(ABI);
  const keccak256 = ethers.keccak256;
  const byId = new Map(model.queue.map((r) => [r.id, r]));
  // The second press of a button belongs to what was on the screen at the first: the call is kept at the first press.
  const confirm = createConfirm();

  const say = (box, words) => { if (box) box.textContent = words; };
  const boxOf = (name) => $(`#so-do-${name}`, page);
  /** Forgets an armed button: the selection or the form it was armed for has changed. */
  const disarm = () => {
    confirm.reset();
    page.querySelectorAll("[data-armed]").forEach((b) => { b.removeAttribute("data-armed"); if (b.dataset.was) b.textContent = b.dataset.was; });
  };

  /** Sends one call as the connected wallet, says how it ended, and draws the page again from the chain. One at a time. */
  async function send(run, done) {
    if (pending.active) return;
    await pending.run(async () => {
      page.querySelectorAll("button").forEach((b) => (b.disabled = true));
      status.className = "sp-status busy";
      status.textContent = "Confirm in your wallet…";
      let words;
      let ok = false;
      try {
        const signed = new ethers.Contract(at, ABI, chain.currentSigner());
        const receipt = await (await run(signed)).wait();
        chain.forgetLiveReads();
        if (!receipt || receipt.status !== 1) throw new Error("the call reverted");
        words = `${done} (transaction ${receipt.hash}).`;
        ok = true;
      } catch (err) {
        words = operatorRefusal(refusalName(err, iface)) || friendly(err);
      }
      try {
        await simOperatorView(at, page);
      } catch (err) {
        words = `${words} The page could not be read again (${friendly(err)}). Reload it to see the queue as the chain has them.`;
      }
      const after = $("#sim-status", page);
      if (after) {
        after.className = `sp-status ${ok ? "ok" : "err"}`;
        after.textContent = words;
      }
    });
  }

  /** A first press says what the call will do and arms the button; the second press, with the same call, sends it. */
  function twoStep(button, boxName, words, label, call, run, done) {
    if (pending.active) return;
    const got = confirm.press(call.data, run);
    if (got.action === "arm") {
      page.querySelectorAll("[data-armed]").forEach((b) => { b.removeAttribute("data-armed"); if (b.dataset.was) b.textContent = b.dataset.was; });
      button.dataset.was = button.textContent;
      button.dataset.armed = "1";
      button.textContent = `Sign: ${label}`;
      say(boxOf(boxName), words);
      return;
    }
    send(got.run, done);
  }

  // ── the approver: the queue ──
  const picked = () => [...page.querySelectorAll(".so-pick:checked")].map((c) => byId.get(Number(c.dataset.id))).filter(Boolean);
  const paintSelection = () => {
    const button = $("#so-approve-sel", page);
    if (!button) return;
    const rows = picked();
    button.textContent = `Approve selected (${rows.length})`;
    const why = $("#so-sel-why", page);
    if (rows.length === 0) {
      why.textContent = "Tick the payouts to approve in one signature.";
      button.disabled = true;
    } else {
      const got = selectionCall({ pool: at, rows, keccak256 });
      why.textContent = got.ok ? "One signature approves them all." : got.problem;
      button.disabled = !got.ok;
    }
    if (pending.active) button.disabled = true;
  };
  page.querySelectorAll(".so-pick").forEach((c) => c.addEventListener("change", () => {
    // Another selection is another call: whatever was armed is forgotten.
    disarm();
    paintSelection();
  }));

  $("#queue", page)?.addEventListener("click", (e) => {
    if (pending.active) return;
    const sel = e.target.closest("#so-approve-sel");
    if (sel && !sel.disabled) {
      const rows = picked();
      const got = selectionCall({ pool: at, rows, keccak256 });
      if (!got.ok) return say(boxOf("queue"), got.problem);
      const ids = got.call.args[0];
      twoStep(sel, "queue", got.words, `approve ${rows.length}`, got.call, (c) => c.approvePayouts(ids), "Approved");
      return;
    }
    const one = e.target.closest("button[data-approve]");
    if (one && !one.disabled) {
      const row = byId.get(Number(one.dataset.approve));
      if (one.dataset.anyway) {
        const call = (() => { try { return approveAnywayCall({ pool: at, row, confirm: window.confirm(HOLD_WARNING) ? HOLD_WARNING : "", keccak256 }); } catch (err) { say(boxOf("queue"), err.message); return null; } })();
        if (call) send((c) => c.approvePayouts(call.args[0]), "Approved");
        return;
      }
      const rows = [row];
      const got = selectionCall({ pool: at, rows, keccak256 });
      if (!got.ok) return say(boxOf("queue"), got.problem);
      const ids = got.call.args[0];
      twoStep(one, "queue", approveWords(rows), `approve #${row.id}`, got.call, (c) => c.approvePayouts(ids), "Approved");
      return;
    }
    if (e.target.closest("#so-more")) {
      reading.set(at, (reading.get(at) || PAYOUT_WINDOW) + PAYOUT_WINDOW);
      simOperatorView(at, page).catch((err) => say(boxOf("queue"), `Could not read more: ${friendly(err)}`));
      return;
    }
    const stop = e.target.closest("button[data-stop-for]");
    if (stop) {
      const select = $("#so-stop-id", page);
      if (select) {
        select.value = stop.dataset.stopFor;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        $("#stop-form", page)?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
  });

  // ── the checker: the form ──
  const form = $("#stop-form", page);
  if (form) {
    const fields = () => ({ id: $("#so-stop-id", page).value, reason: (page.querySelector("input[name=so-reason]:checked") || {}).value,
      paired: $("#so-paired", page).value, trades: $("#so-trades", page).value, links: $("#so-links", page).value });
    const built = () => {
      const f = fields();
      const kind = STOP_FORM.find((x) => x.reason === Number(f.reason));
      if (!f.id || !kind) return { f, got: null };
      return { f, got: stopFromForm({ pool: at, fields: { ...f, links: kind.links === "none" ? "" : f.links, paired: kind.paired === "none" ? "" : f.paired }, keccak256 }) };
    };
    const paint = () => {
      const f = fields();
      const kind = STOP_FORM.find((x) => x.reason === Number(f.reason));
      $("#so-links-label", page).textContent = !kind ? "Links" : kind.links === "none" ? "Links (none for this reason)"
        : kind.links === "transactions" ? "Transactions that link the wallets" : "Hash of the published text";
      $("#so-links", page).disabled = kind?.links === "none";
      $("#so-paired", page).disabled = kind?.paired === "none";
      page.querySelectorAll(".so-reason").forEach((l) => l.classList.toggle("on", l.dataset.reason === String(f.reason)));
      const go = $("#so-stop-go", page);
      const box = boxOf("stop");
      const { got } = built();
      if (!got) { go.disabled = true; say(box, "Choose a payout and a reason."); return; }
      go.disabled = !got.ok || pending.active;
      say(box, got.ok ? stopWords(byId.get(Number(f.id)), f.reason, model.now) : got.problem);
    };
    // Any change to the form is a different stop: whatever was armed is forgotten.
    const changed = () => { disarm(); paint(); };
    form.addEventListener("input", changed);
    form.addEventListener("change", changed);
    $("#so-stop-go", page).addEventListener("click", () => {
      if (pending.active) return;
      const { f, got } = built();
      if (!got || !got.ok) return;
      const args = got.call.args;
      const button = $("#so-stop-go", page);
      twoStep(button, "stop", stopWords(byId.get(Number(f.id)), f.reason, model.now), "write stop", got.call, (c) => c.stopPayout(...args), "Stop written");
    });
    paint();
  }

  // ── the checker: lifting a stop, making it final; the closer: cancelling a request ──
  $("#stops", page)?.addEventListener("click", (e) => {
    const lift = e.target.closest("button[data-lift]");
    const fin = e.target.closest("button[data-final]");
    const button = lift || fin;
    if (!button || button.disabled || pending.active) return;
    const stop = model.stops.find((s) => s.id === Number(lift ? lift.dataset.lift : fin.dataset.final));
    const id = stop.id;
    const call = { data: `${lift ? "lift" : "final"}:${id}` };
    if (lift) twoStep(button, "stops", liftWords(stop), `lift #${id}`, call, (c) => c.liftStop(id), "Stop lifted");
    else twoStep(button, "stops", finalizeWords(stop), `final #${id}`, call, (c) => c.finalizeStop(id), "Stop made final");
  });
  $("#stops", page)?.addEventListener("click", (e) => {
    if (!e.target.closest("#so-stops-more") || pending.active) return;
    moreStops.set(at, MORE_CALLS);
    simOperatorView(at, page).catch((err) => say(boxOf("stops"), `Could not read more: ${friendly(err)}`));
  });
  $("#requests", page)?.addEventListener("click", (e) => {
    const button = e.target.closest("button[data-cancel]");
    if (!button || button.disabled || pending.active) return;
    twoStep(button, "requests", cancelWords(model.request), "cancel request", { data: "cancel" }, (c) => c.cancelWithdraw(), "Request cancelled");
  });
}
