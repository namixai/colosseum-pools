// A simulated pool, the control panel, owner's section: the switch of each line and the pool's money as the owner
// reads it. A page of its own at #/manage/<pool>: no link to it from the trader's navigation, and none from the
// trader's panel. The wallet gives the account and the pool's `creator()` the role; only the creator sees the
// switches. The contract holds the right either way (`setLine` is `onlyCreator`): this page only shows it.
// Withdrawing and finishing, the codes and the operator's section are the panel's next sections (SECTIONS).
import * as chain from "../lib/chain.js";
import { esc, render, $, friendly, refusalName } from "../lib/ui.js";
import { SIM_POOL_ABI } from "../lib/simpool.js";
import {
  SIM_LINES_ABI, SIM_OWNER_ABI, menuRows, roleOf, ownerMoney, moneyHtml, switchesHtml, poolRefusal,
} from "../lib/simlines.js";
import { PANEL_SECTIONS, panelNavHtml, notOwnerHtml, noWalletHtml } from "../lib/simpanel.js";

const { ethers } = window;
const ABI = [...SIM_POOL_ABI, ...SIM_LINES_ABI, ...SIM_OWNER_ABI];

function nav(address) {
  return panelNavHtml(address, "lines", PANEL_SECTIONS);
}

/** What the owner's section reads: the lines with their `lineState`, and the pool's money. Read only for the creator. */
export async function readOwnerPool(address) {
  const pool = new ethers.Contract(address, ABI, chain.readProvider);
  const [menu, tiers, free, floor, sharesDue, principal, closed] = await Promise.all([
    pool.menu(), pool.tiers(), pool.free(), pool.floor(), pool.sharesDue(), pool.principal(), pool.closed(),
  ]);
  const states = await chain.readAll(menu, (_, line) => pool.lineState(line));
  return { rows: menuRows({ menu, tiers, states }), closed, money: ownerMoney({ free, principal, sharesDue, floor }) };
}

export async function simAdminView(address, page) {
  const at = ethers.getAddress(address);
  const pool = new ethers.Contract(at, ABI, chain.readProvider);
  const creator = await pool.creator();
  const role = roleOf(chain.currentAddress(), creator);
  if (role !== "owner") {
    // Nothing of the pool is read for anyone else: the panel says what it is, and who it is for.
    render(page, role === "no-wallet" ? noWalletHtml() : notOwnerHtml());
    const connect = $("#panel-connect", page);
    if (connect) connect.addEventListener("click", () => chain.connect().catch((err) => alert(friendly(err))));
    return;
  }
  const data = await readOwnerPool(at);
  render(page, `
    ${nav(at)}
    <section class="card pool-head">
      <h2>Pool ${esc(chain.short(at))} · owner ${data.closed ? `<span class="badge bad">Closed</span>` : `<span class="badge ok">Open</span>`}</h2>
      ${moneyHtml(data.money)}
    </section>
    <section class="card">
      <h3>Lines</h3>
      <p class="muted">A line sells only while it is switched on here and the pool has the money for two of its payouts.
        You can switch a line off at any time. You can switch it on only when the pool has that money; no line switches itself on.</p>
      <div id="sim-switches">${switchesHtml(data.rows)}</div>
      <p id="sim-status" class="result" role="status"></p>
    </section>`);

  const status = $("#sim-status", page);
  $("#sim-switches", page).addEventListener("click", async (e) => {
    const button = e.target.closest("button.sim-switch");
    if (!button || button.disabled) return;
    const line = Number(button.dataset.line);
    const on = button.dataset.next === "true";
    page.querySelectorAll("button.sim-switch").forEach((b) => (b.disabled = true));
    status.className = "result busy";
    status.textContent = "Working…";
    try {
      if (!chain.currentSigner()) await chain.connect();
      const signed = new ethers.Contract(at, ABI, chain.currentSigner());
      const receipt = await (await signed.setLine(line, on)).wait();
      chain.forgetLiveReads();
      if (!receipt || receipt.status !== 1) throw new Error("the switch reverted");
      await simAdminView(at, page);
      const after = $("#sim-status", page);
      after.className = "result ok";
      after.textContent = `Line ${line} switched ${on ? "on" : "off"} (transaction ${receipt.hash}).`;
    } catch (err) {
      status.className = "result err";
      const iface = new ethers.Interface(SIM_LINES_ABI);
      status.textContent = poolRefusal(refusalName(err, iface), err?.revert?.args ? [...err.revert.args] : []) || friendly(err);
      // The switches come back as the chain has them now, not as they were before the refusal.
      await simAdminView(at, page);
      const after = $("#sim-status", page);
      after.className = "result err";
      after.textContent = status.textContent;
    }
  });
}
