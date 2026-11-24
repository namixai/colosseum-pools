// A pool of exams, the user panel: the menu of the pool (six sizes by four tiers), what each line costs, and the
// purchase. Reading, and buying: nothing here manages the pool (its owner's panel is a page of its own,
// views/simadmin.js, with no link from this one). Built to mockup 01: no payout column, no
// strip of facts, no gauge. The states and the words are app/lib/simlines.js; this file reads the pool and draws.
import * as chain from "../lib/chain.js";
import { esc, render, $, friendly, refusalName } from "../lib/ui.js";
import { SIM_POOL_ABI, SIM_FACTORY_ABI, codeHash, purchase } from "../lib/simpool.js";
import {
  SIM_LINES_ABI, TERMS_BOOK_ABI, menuRows, menuHtml, poolRefusal, termsText, isSet,
} from "../lib/simlines.js";
import { userHeaderHtml, footerHtml } from "../lib/simpanel.js";
import { accountLabel, wireShell } from "./simshell.js";
import { keepFromReceipt } from "./simtrader.js";
import { networkMarks } from "../lib/examscreen.js";
import { networkName } from "../lib/network.js";
import { payToken } from "../lib/simsite.js";
import { CONFIG } from "../config.js";

const { ethers } = window;
const ABI = [...SIM_POOL_ABI, ...SIM_LINES_ABI];
const ERC20_ABI = [
  "function allowance(address owner, address spender) view returns (uint256)",
  "function approve(address spender, uint256 amount) returns (bool)",
];

/** What the pool's page reads: the menu with each line's tier and `lineState`, the free money and the terms text. */
export async function readSimPool(address) {
  const pool = new ethers.Contract(address, ABI, chain.readProvider);
  const [menu, tiers, free, closed, factory, terms] = await Promise.all([
    pool.menu(), pool.tiers(), pool.free(), pool.closed(), pool.factory(), pool.terms(),
  ]);
  const states = await chain.readAll(menu, (_, line) => pool.lineState(line));
  const rows = menuRows({ menu, tiers, states, closed });
  // The terms are the book's text under the pool's hash. A pool whose book cannot be read says so: nothing is made up.
  let text = null;
  try {
    const book = await new ethers.Contract(factory, SIM_FACTORY_ABI, chain.readProvider).termsBook();
    if (isSet(book)) text = termsText(terms, await new ethers.Contract(book, TERMS_BOOK_ABI, chain.readProvider).text(terms));
  } catch {
    text = null;
  }
  return { rows, free, terms, termsText: text };
}

/** The page: `address` is the pool, `query` the link's `?code=` (the group's code the trader was given). */
export async function simPoolView(address, query, page) {
  const at = ethers.getAddress(address);
  const data = await readSimPool(at);
  const given = new URLSearchParams(query || "").get("code") || "";
  const marks = networkMarks(CONFIG);
  render(page, `<div class="sp sp-user">
    ${userHeaderHtml({ address: at, account: accountLabel(), marks })}
    <section class="sp-hero">
      <div class="sp-kick">Pool ${esc(chain.short(at))} · ${esc(networkName())}</div>
      <h1>How deep<br>is <em>the pool?</em></h1>
      <p class="sp-lede"><b>Depth = money the pool has free for payouts.</b> Each line is an exam: pass it, then pass the stage on the same size,
        and the pool pays out. A line sells only while the pool holds <span class="sp-mk">two of its payouts</span> free.</p>
    </section>
    <div id="sim-menu">${menuHtml(data.rows, data.free, given)}</div>
    <div class="sp-codebar">
      <label>Trading group code <input id="sim-code" type="text" autocomplete="off" spellcheck="false" value="${esc(given)}" placeholder="enter your group's code"></label>
      <span class="sp-why">Every exam is bought with a trading group's code.</span>
    </div>
    <p id="sim-status" class="sp-status" role="status"></p>
    <details class="sp-terms"><summary>Terms of this pool</summary>
      ${data.termsText ? `<pre>${esc(data.termsText)}</pre>` : `<p class="sp-why">This pool's terms text could not be read from the chain.</p>`}
      <p class="sp-why">terms hash ${esc(data.terms)}</p></details>
    ${footerHtml({ marks })}</div>`);
  wireShell(page);

  const status = $("#sim-status", page);
  const input = $("#sim-code", page);
  // One purchase at a time: while one is pending the menu is redrawn with its buttons off, and a click is not taken.
  let busy = false;
  const repaint = () => {
    $("#sim-menu", page).innerHTML = menuHtml(data.rows, data.free, input.value);
    if (busy) page.querySelectorAll("button.sp-buy").forEach((b) => (b.disabled = true));
  };
  input.addEventListener("input", repaint);
  $("#sim-menu", page).addEventListener("click", async (e) => {
    const button = e.target.closest("button.sp-buy");
    if (!button || button.disabled || busy) return;
    const line = Number(button.dataset.line);
    // Connecting draws the page again (app.js listens to the account), so it comes first and alone: the trader presses
    // Buy again on the page that knows the wallet. Nothing is written to nodes the redraw would take away.
    if (!chain.currentSigner()) {
      try {
        await chain.connect();
      } catch (err) {
        status.className = "sp-status err";
        status.textContent = friendly(err);
      }
      return;
    }
    busy = true;
    repaint();
    status.className = "sp-status busy";
    status.textContent = "Working…";
    try {
      status.textContent = await buy(at, data, line, input.value);
      status.className = "sp-status ok";
    } catch (err) {
      status.className = "sp-status err";
      status.textContent = answer(err);
    } finally {
      busy = false;
      repaint();
    }
  });
}

/** The pool's own refusal in words, or the wallet's and the node's as the app words them. */
function answer(err) {
  const name = refusalName(err, new ethers.Interface(SIM_LINES_ABI));
  const args = err?.revert?.args ? [...err.revert.args] : [];
  return poolRefusal(name, args) || friendly(err);
}

/** Buys `line` with the group's code: checks the code against the pool, approves the price if it must, and buys. */
async function buy(address, data, line, codeText) {
  const signer = chain.currentSigner();
  const pool = new ethers.Contract(address, ABI, signer);
  // Not trimmed inside the library and not folded: a code is its text exactly. Stray spaces at the ends of the input are not part of it.
  const code = codeHash(codeText.trim(), ethers.keccak256);
  const set = await pool.codeOf(code);
  const { args, price } = purchase({ menu: data.rows, line, code, codes: [{ code, partner: set.partner }] });
  // The token is the pool's own answer, held to the settings' USDC on mainnet before any allowance is asked for.
  const usdc = new ethers.Contract(payToken(await pool.usdc()), ERC20_ABI, signer);
  if ((await usdc.allowance(signer.address, address)) < price) await (await usdc.approve(address, price)).wait();
  const receipt = await (await pool.buy(...args)).wait();
  chain.forgetLiveReads();
  if (!receipt || receipt.status !== 1) throw new Error("the purchase reverted");
  // The exam bought is kept in this browser, so My exams lists it however long ago it was bought.
  keepFromReceipt(address, signer.address, receipt);
  return `Bought: the exam is yours (transaction ${receipt.hash}).`;
}
