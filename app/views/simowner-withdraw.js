// The control panel's "Withdraw & finish" (mockup 02): what the owner can take out now, the request to withdraw and its
// wait, putting more money in, naming the approver, and closing and finishing the pool. Every control says what it
// will do before it is pressed, and a control the contract would refuse is held, with the reason and the date. Closing
// and finishing end things for good, so they take a second press. The pool's contract refuses what is not allowed
// whatever this page does.
import * as chain from "../lib/chain.js";
import { $ } from "../lib/ui.js";
import { utc } from "../lib/simpool.js";
import {
  requestState, requestPlan, addPlan, approverPlan, closeState, finishState, receiptFacts, afterWithdrawWords,
  withdrawHtml,
} from "../lib/simowner.js";
import { poolAt, runOwner, mined, eventsOf, failedBoxHtml } from "./simowner-run.js";

const { ethers } = window;
const ERC20 = [
  "function allowance(address owner, address spender) view returns (uint256)",
  "function approve(address spender, uint256 amount) returns (bool)",
];

/** What the section reads: the pool's own answers, and the chain's clock. */
export async function readWithdraw(at) {
  try {
    const pool = poolAt(at);
    const [withdrawable, principal, requested, readyAt, approver, lockedUntil, closed, closedAt, closedEarly, finished, live, owed, reserved, now] = await Promise.all([
      pool.withdrawable(), pool.principal(), pool.requested(), pool.readyAt(), pool.approver(), pool.lockedUntil(), pool.closed(),
      pool.closedAt(), pool.closedEarly(), pool.finished(), pool.live(), pool.owed(), pool.reserved(), chain.blockTime(),
    ]);
    const reads = { requested, readyAt, closed, closedAt, closedEarly, finished, lockedUntil, live, owed, reserved };
    return { now, money: { withdrawable, principal }, approver, standing: requestState(reads, now, withdrawable),
      close: closeState(reads, now), finish: finishState(reads, now) };
  } catch (err) {
    return { error: err };
  }
}

/** The section's markup from what `readWithdraw` answered. */
export function withdrawSection(data) {
  if (data.error) return failedBoxHtml("withdraw", "Withdraw & finish", data.error);
  return withdrawHtml({ money: data.money, request: data.standing, approver: data.approver, close: data.close, finish: data.finish, now: data.now });
}

/** A button that takes two presses within a few seconds: the first says what the second does. */
function twoPresses(button, then) {
  let timer = null;
  const label = button.textContent;
  const disarm = () => { clearTimeout(timer); timer = null; button.textContent = label; button.classList.remove("armed"); };
  button.addEventListener("click", () => {
    if (button.disabled) return;
    if (!timer) {
      button.textContent = "Press again to confirm";
      button.classList.add("armed");
      timer = setTimeout(disarm, 8000);
      return;
    }
    disarm();
    then();
  });
}

/** Wires the section. `ctx.refresh()` draws the whole page again from the chain. */
export function wireWithdraw(page, ctx, data) {
  if (data.error || !$("#withdraw", page)) return;
  const run = (task) => runOwner(page, ctx, "so-wd-status", task);
  // A finished pool takes no request, no money and no approver: the fields are held and the contract would refuse (`Done`).
  if (data.finish.kind === "finished") page.querySelectorAll("#withdraw input").forEach((input) => { input.disabled = true; });

  // Each amount or address field says what its call will do as it is typed, and holds its button until it can be sent.
  const live = (form, button, words, plan) => {
    const update = () => {
      const p = plan();
      const box = $(words, page);
      box.className = `so-plan${p.ok ? "" : " bad"}`;
      box.textContent = p.ok ? p.words : (form.querySelector("input").value ? p.error : "");
      button.disabled = !p.ok;
    };
    form.addEventListener("input", update);
    return update;
  };

  const requestForm = $("#so-request-form", page);
  live(requestForm, $("#so-request", page), "#so-request-words",
    () => requestPlan({ text: requestForm.elements.amount.value, withdrawable: data.money.withdrawable, standing: data.standing, now: data.now }));
  requestForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const plan = requestPlan({ text: requestForm.elements.amount.value, withdrawable: data.money.withdrawable, standing: data.standing, now: data.now });
    if (!plan.ok) return;
    run(async (signed, iface, say) => {
      say(`${plan.words} Sign it in your wallet.`);
      const receipt = await mined(signed.requestWithdraw(plan.amount), "the request");
      const ev = eventsOf(receipt, ctx.at, iface).find((x) => x.name === "WithdrawRequested");
      const when = ev ? utc(ev.args.readyAt) : "";
      return `Requested (transaction ${receipt.hash}).${when ? ` You can carry it out no sooner than ${when}.` : ""}`;
    });
  });

  const withdraw = $("#so-withdraw", page);
  if (withdraw) {
    withdraw.addEventListener("click", () => run(async (signed, iface, say) => {
      say("Carrying out your request. Sign it in your wallet.");
      const receipt = await mined(signed.withdraw(), "the withdrawal");
      return afterWithdrawWords(receiptFacts(eventsOf(receipt, ctx.at, iface)), receipt.hash);
    }));
  }
  const cancel = $("#so-cancel", page);
  if (cancel) {
    cancel.addEventListener("click", () => run(async (signed, _iface, say) => {
      say("Cancelling the request. Nothing moves. Sign it in your wallet.");
      const receipt = await mined(signed.cancelWithdraw(), "the cancellation");
      return `The request is cancelled (transaction ${receipt.hash}). Nothing moved.`;
    }));
  }

  const addForm = $("#so-add-form", page);
  live(addForm, $("#so-add", page), "#so-add-words", () => addPlan({ text: addForm.elements.amount.value, principal: data.money.principal }));
  addForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const plan = addPlan({ text: addForm.elements.amount.value, principal: data.money.principal });
    if (!plan.ok) return;
    run(async (signed, _iface, say) => {
      const usdc = new ethers.Contract(await poolAt(ctx.at).usdc(), ERC20, chain.currentSigner());
      if ((await usdc.allowance(chain.currentAddress(), ctx.at)) < plan.amount) {
        say("Step 1 of 2: allow the pool to take the money. Sign it in your wallet.");
        await mined(usdc.approve(ctx.at, plan.amount), "the allowance");
      }
      say("Step 2 of 2: the pool takes the money. Sign it in your wallet.");
      const receipt = await mined(signed.addToPot(plan.amount), "adding the money");
      return `The money is in the pool (transaction ${receipt.hash}). It is your principal: it is never shared with partners.`;
    });
  });

  const approverForm = $("#so-approver-form", page);
  live(approverForm, $("#so-approver", page), "#so-approver-words", () => approverPlan({ text: approverForm.elements.approver.value, current: data.approver }));
  approverForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const plan = approverPlan({ text: approverForm.elements.approver.value, current: data.approver });
    if (!plan.ok) return;
    run(async (signed, _iface, say) => {
      say(`${plan.words} Sign it in your wallet.`);
      const receipt = await mined(signed.setApprover(plan.address), "naming the approver");
      return plan.none ? `The approver is removed (transaction ${receipt.hash}).` : `The approver is named (transaction ${receipt.hash}).`;
    });
  });

  const close = $("#so-close", page);
  if (close && !close.disabled) {
    twoPresses(close, () => run(async (signed, _iface, say) => {
      say("Closing the pool to new sales. Sign it in your wallet.");
      const receipt = await mined(signed.close(), "closing");
      return `The pool is closed to new sales (transaction ${receipt.hash}). It can finish a week from now, once nothing is running or owed.`;
    }));
  }
  const finish = $("#so-finish", page);
  if (finish && !finish.disabled) {
    twoPresses(finish, () => run(async (signed, iface, say) => {
      say("Finishing the pool. Sign it in your wallet.");
      const receipt = await mined(signed.finish(), "finishing");
      return `The pool is finished. ${afterWithdrawWords(receiptFacts(eventsOf(receipt, ctx.at, iface)), receipt.hash)}`;
    }));
  }
}
