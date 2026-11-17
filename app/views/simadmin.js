// A pool of exams, the control panel, owner's section: the pool's money in four parts and the switch of each line.
// A page of its own at #/manage/<pool> with a header of its own (mockup 02): no link to it from
// the site's navigation, and none from the user panel. The wallet gives the account and the pool's `creator()` the
// role; only the creator sees the money and the switches. The contract holds the right either way (`setLine` is
// `onlyCreator`): this page only shows it. Codes, withdrawing and finishing are the panel's next sections.
import * as chain from "../lib/chain.js";
import { esc, render, $, friendly, refusalName } from "../lib/ui.js";
import { SIM_POOL_ABI } from "../lib/simpool.js";
import {
  SIM_LINES_ABI, SIM_OWNER_ABI, menuRows, roleOf, ownerMoney, moneyHtml, switchesHtml, poolRefusal,
} from "../lib/simlines.js";
import { manageHeaderHtml, footerHtml, notOwnerHtml, noWalletHtml } from "../lib/simpanel.js";
import { accountLabel, wireShell } from "./simshell.js";

const { ethers } = window;
const ABI = [...SIM_POOL_ABI, ...SIM_LINES_ABI, ...SIM_OWNER_ABI];

/** What the owner's section reads: the lines with their `lineState`, and the pool's money in its parts. */
export async function readOwnerPool(address) {
  const pool = new ethers.Contract(address, ABI, chain.readProvider);
  const [menu, tiers, free, closed, floor, owed, reserved, held, staged, sharesDue] = await Promise.all([
    pool.menu(), pool.tiers(), pool.free(), pool.closed(), pool.floor(), pool.owed(), pool.reserved(), pool.held(), pool.staged(), pool.sharesDue(),
  ]);
  const states = await chain.readAll(menu, (_, line) => pool.lineState(line));
  return { rows: menuRows({ menu, tiers, states, closed }),
    money: ownerMoney({ free, owed, reserved, held, staged, sharesDue, floor }) };
}

export async function simAdminView(address, page) {
  const at = ethers.getAddress(address);
  const pool = new ethers.Contract(at, ABI, chain.readProvider);
  const creator = await pool.creator();
  const role = roleOf(chain.currentAddress(), creator);
  const name = `Pool ${chain.short(at)}`;
  const header = manageHeaderHtml({ address: at, account: accountLabel(), role, poolName: name });
  if (role !== "owner") {
    // Nothing of the pool is read for anyone else: the panel says what it is, and who it is for.
    render(page, `<div class="sp sp-manage">${header}${role === "no-wallet" ? noWalletHtml() : notOwnerHtml()}${footerHtml()}</div>`);
    wireShell(page);
    return;
  }
  const data = await readOwnerPool(at);
  render(page, `<div class="sp sp-manage">${header}
    <section class="sp-hero sp-hd">
      <div class="sp-kick">Owner panel · ${esc(name)} · Hyperliquid testnet</div>
      <h1>The pool's <em>water level</em></h1>
      <p class="sp-lede">Water level = the pool's money. A line can sell, or be switched on, only while the free money covers <span class="sp-mk">two of its payouts</span>.</p>
    </section>
    ${moneyHtml(data.money)}
    <div id="sim-switches">${switchesHtml(data.rows, data.money.free)}</div>
    <p id="sim-status" class="sp-status" role="status"></p>
    ${footerHtml()}</div>`);
  wireShell(page);

  const status = $("#sim-status", page);
  $("#sim-switches", page).addEventListener("click", async (e) => {
    const button = e.target.closest("button.sp-tg");
    if (!button || button.disabled) return;
    const line = Number(button.dataset.line);
    const on = button.dataset.next === "true";
    page.querySelectorAll("button.sp-tg").forEach((b) => (b.disabled = true));
    status.className = "sp-status busy";
    status.textContent = "Working…";
    let words;
    let ok = false;
    try {
      const signed = new ethers.Contract(at, ABI, chain.currentSigner());
      const receipt = await (await signed.setLine(line, on)).wait();
      chain.forgetLiveReads();
      if (!receipt || receipt.status !== 1) throw new Error("the switch reverted");
      words = `Line ${line} switched ${on ? "on" : "off"} (transaction ${receipt.hash}).`;
      ok = true;
    } catch (err) {
      const iface = new ethers.Interface(SIM_LINES_ABI);
      words = poolRefusal(refusalName(err, iface), err?.revert?.args ? [...err.revert.args] : []) || friendly(err);
    }
    // The switches come back as the chain has them now, whether the switch went through or was refused. A page that
    // cannot be read again (the node's rate, say) keeps the old one, its switches as they were, and says so: a switch
    // that was sent is not reported as an error for that.
    try {
      await simAdminView(at, page);
    } catch (err) {
      page.querySelectorAll("button.sp-tg").forEach((b) => (b.disabled = b.classList.contains("dis")));
      words = `${words} The page could not be read again (${friendly(err)}). Reload it to see the switches as the chain has them.`;
    }
    const after = $("#sim-status", page);
    if (after) {
      after.className = `sp-status ${ok ? "ok" : "err"}`;
      after.textContent = words;
    }
  });
}
