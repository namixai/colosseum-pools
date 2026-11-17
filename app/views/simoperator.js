// A pool of exams, the control panel's operator section (#/manage/<pool>/operator), built to mockup 04. It is a page of the
// management panel, apart from the owner's page and from the user panel: the wallet's roles are read from the chain (the
// pool's `approver()`, the factory's `checker()` and `closer()`), a wallet with none sees one sentence, and each role is
// drawn only the controls its contract call allows. Before a call is sent the page says in words what it will do. The
// words, the queue's states and the calls are app/lib/simoperator.js and app/lib/operator.js; this file reads and wires.
import * as chain from "../lib/chain.js";
import { esc, render, $, friendly, refusalName } from "../lib/ui.js";
import { SIM_POOL_ABI, SIM_POOL_STOP_RESERVE_ABI, SIM_POOL_APPROVAL_ABI } from "../lib/simpool.js";
import { SIM_LINES_ABI } from "../lib/simlines.js";
import { OPERATOR_ABI } from "../lib/operator.js";
import { footerHtml } from "../lib/simpanel.js";
import {
  SIM_OPERATOR_ABI, SIM_OPERATOR_FACTORY_ABI, PAYOUT_WINDOW, mergeAbis, operatorRoles, operatorModel, operatorHeaderHtml,
  operatorHtml, messageHtml, selectionCall, approveAnywayCall, HOLD_WARNING, stopFromForm, stopWords, liftWords,
  finalizeWords, cancelWords, operatorRefusal, approveWords, STOP_FORM,
} from "../lib/simoperator.js";
import { accountLabel, wireShell } from "./simshell.js";

const { ethers } = window;
// `payout(id)` is named by three of the libraries with different fields: the widest comes first.
const ABI = mergeAbis(SIM_POOL_APPROVAL_ABI, SIM_POOL_STOP_RESERVE_ABI, SIM_OPERATOR_ABI, OPERATOR_ABI, SIM_LINES_ABI, SIM_POOL_ABI);
const ZERO = "0x0000000000000000000000000000000000000000";

/** The pool's approver, or nobody for a pool that does not answer `approver()`. */
async function approverOf(pool) {
  try {
    return await pool.approver();
  } catch (err) {
    if (err?.code === "CALL_EXCEPTION") return ZERO;
    throw err;
  }
}

/** What the operator's page reads of the pool, for the roles the wallet has. */
export async function readOperatorPool(address, roles) {
  const pool = new ethers.Contract(address, ABI, chain.readProvider);
  const now = await chain.blockTime();
  const out = { now };
  const queue = roles.includes("approver") || roles.includes("checker");
  if (queue) {
    const [count, perDay, room, nextPayAt, menu, tiers] = await Promise.all([
      pool.payoutCount(), pool.payoutsPerDay(), pool.payRoom(), pool.nextPayAt(), pool.menu(), pool.tiers(),
    ]);
    const firstId = Math.max(0, Number(count) - PAYOUT_WINDOW);
    const ids = Array.from({ length: Number(count) - firstId }, (_, i) => firstId + i);
    const payouts = await chain.readAll(ids, (id) => pool.payout(id));
    const live = payouts.filter((p) => Number(p.state) === 1 || Number(p.state) === 2 || Number(p.state) === 5);
    const accounts = [...new Set(live.map((p) => p.account.toLowerCase()))];
    const lines = await chain.readAll(accounts, async (a) => (await pool.accountOf(a)).line);
    const stoppedIds = ids.filter((id, i) => Number(payouts[i].state) === 2 || Number(payouts[i].state) === 5);
    const why = await chain.readAll(stoppedIds, (id) => pool.stopReasonOf(id));
    Object.assign(out, { payouts, firstId, payoutsPerDay: perDay, payRoom: room, nextPayAt, menu, tiers,
      accountLines: new Map(accounts.map((a, i) => [a, Number(lines[i])])),
      reasons: new Map(stoppedIds.map((id, i) => [id, Number(why[i])])) });
  } else {
    Object.assign(out, { payouts: [], firstId: 0, payoutsPerDay: 0, payRoom: 0, nextPayAt: 0, menu: [], tiers: [] });
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
  const reads = await readOperatorPool(at, who.roles);
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
  let armed = null;

  const say = (box, words) => { if (box) box.textContent = words; };
  const boxOf = (name) => $(`#so-do-${name}`, page);

  /** Sends one call as the connected wallet, says how it ended, and draws the page again from the chain. */
  async function send(run, done) {
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
  }

  /** A first press says what the call will do and arms the button; the second press sends it. */
  function twoStep(button, boxName, words, label, run, done) {
    const key = `${boxName}:${label}`;
    if (armed !== key) {
      armed = key;
      page.querySelectorAll("[data-armed]").forEach((b) => { b.removeAttribute("data-armed"); b.textContent = b.dataset.was || b.textContent; });
      button.dataset.was = button.textContent;
      button.dataset.armed = "1";
      button.textContent = `Sign: ${label}`;
      say(boxOf(boxName), words);
      return;
    }
    armed = null;
    send(run, done);
  }

  // ── the approver: the queue ──
  const picked = () => [...page.querySelectorAll(".so-pick:checked")].map((c) => byId.get(Number(c.dataset.id))).filter(Boolean);
  const paintSelection = () => {
    const button = $("#so-approve-sel", page);
    if (!button) return;
    const rows = picked();
    button.textContent = `Approve selected (${rows.length})`;
    button.disabled = rows.length === 0;
    const why = $("#so-sel-why", page);
    if (rows.length === 0) why.textContent = "Tick the payouts to approve in one signature.";
    else {
      const got = selectionCall({ pool: at, rows, keccak256 });
      why.textContent = got.ok ? "One signature approves them all." : got.problem;
      button.disabled = !got.ok;
    }
  };
  page.querySelectorAll(".so-pick").forEach((c) => c.addEventListener("change", paintSelection));

  $("#queue", page)?.addEventListener("click", (e) => {
    const sel = e.target.closest("#so-approve-sel");
    if (sel && !sel.disabled) {
      const rows = picked();
      const got = selectionCall({ pool: at, rows, keccak256 });
      if (!got.ok) return say(boxOf("queue"), got.problem);
      twoStep(sel, "queue", got.words, `approve ${rows.length}`, (c) => c.approvePayouts(got.call.args[0]), "Approved");
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
      twoStep(one, "queue", approveWords([row]), `approve #${row.id}`, (c) => c.approvePayouts([row.id]), "Approved");
      return;
    }
    const stop = e.target.closest("button[data-stop-for]");
    if (stop) {
      const select = $("#so-stop-id", page);
      if (select) {
        select.value = stop.dataset.stopFor;
        select.dispatchEvent(new Event("change"));
        $("#stop-form", page)?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
  });

  // ── the checker: the form ──
  const form = $("#stop-form", page);
  if (form) {
    const fields = () => ({ id: $("#so-stop-id", page).value, reason: (page.querySelector("input[name=so-reason]:checked") || {}).value,
      paired: $("#so-paired", page).value, trades: $("#so-trades", page).value, links: $("#so-links", page).value });
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
      if (!f.id || !f.reason) { go.disabled = true; say(box, "Choose a payout and a reason."); return; }
      const got = stopFromForm({ pool: at, fields: { ...f, links: kind.links === "none" ? "" : f.links, paired: kind.paired === "none" ? "" : f.paired }, keccak256 });
      go.disabled = !got.ok;
      const row = byId.get(Number(f.id));
      say(box, got.ok ? stopWords(row, f.reason, model.now) : got.problem);
    };
    form.addEventListener("input", paint);
    form.addEventListener("change", paint);
    $("#so-stop-go", page).addEventListener("click", () => {
      const f = fields();
      const kind = STOP_FORM.find((x) => x.reason === Number(f.reason));
      const got = stopFromForm({ pool: at, fields: { ...f, links: kind.links === "none" ? "" : f.links, paired: kind.paired === "none" ? "" : f.paired }, keccak256 });
      if (!got.ok) return;
      send((c) => c.stopPayout(...got.call.args), "Stop written");
    });
    paint();
  }

  // ── the checker: lifting a stop, making it final; the closer: cancelling a request ──
  $("#stops", page)?.addEventListener("click", (e) => {
    const lift = e.target.closest("button[data-lift]");
    const fin = e.target.closest("button[data-final]");
    const button = lift || fin;
    if (!button || button.disabled) return;
    const stop = model.stops.find((s) => s.id === Number(lift ? lift.dataset.lift : fin.dataset.final));
    if (lift) twoStep(button, "stops", liftWords(stop), `lift #${stop.id}`, (c) => c.liftStop(stop.id), "Stop lifted");
    else twoStep(button, "stops", finalizeWords(stop), `final #${stop.id}`, (c) => c.finalizeStop(stop.id), "Stop made final");
  });
  $("#requests", page)?.addEventListener("click", (e) => {
    const button = e.target.closest("button[data-cancel]");
    if (!button || button.disabled) return;
    twoStep(button, "requests", cancelWords(model.request), "cancel request", (c) => c.cancelWithdraw(), "Request cancelled");
  });
}
