// A simulated pool, the trader's panel: the menu of the pool (six sizes by four tiers), what each line costs and pays,
// the pool's money, the pool's terms as the chain holds them, and the purchase. Reading, and buying: nothing here
// manages the pool (its owner's panel is a page of its own, views/simadmin.js, with no link from this one).
// The reads and the words are app/lib/simlines.js and app/lib/simpool.js; this file only reads the pool and draws.
import * as chain from "../lib/chain.js";
import { esc, render, $, friendly, refusalName } from "../lib/ui.js";
import { SIM_POOL_ABI, SIM_FACTORY_ABI, codeHash, purchase } from "../lib/simpool.js";
import {
  SIM_LINES_ABI, TERMS_BOOK_ABI, menuRows, traderMoney, moneyHtml, menuHtml, poolRefusal, termsText, isSet,
} from "../lib/simlines.js";

const { ethers } = window;
const ERC20_ABI = [
  "function allowance(address owner, address spender) view returns (uint256)",
  "function approve(address spender, uint256 amount) returns (bool)",
];

/** What the pool's page reads: the menu with each line's tier and `lineState`, the pool's money and its terms text. */
export async function readSimPool(address) {
  const pool = new ethers.Contract(address, [...SIM_POOL_ABI, ...SIM_LINES_ABI], chain.readProvider);
  const [menu, tiers, closed, free, floor, held, factory, terms] = await Promise.all([
    pool.menu(), pool.tiers(), pool.closed(), pool.free(), pool.floor(), pool.held(), pool.factory(), pool.terms(),
  ]);
  const states = await chain.readAll(menu, (_, line) => pool.lineState(line));
  const rows = menuRows({ menu, tiers, states });
  // The terms are the book's text under the pool's hash. A pool whose book cannot be read says so: nothing is made up.
  let text = null;
  try {
    const book = await new ethers.Contract(factory, SIM_FACTORY_ABI, chain.readProvider).termsBook();
    if (isSet(book)) text = termsText(terms, await new ethers.Contract(book, TERMS_BOOK_ABI, chain.readProvider).text(terms));
  } catch {
    text = null;
  }
  return { rows, closed, money: traderMoney({ free, floor, held }), terms, termsText: text };
}

/** The page: `address` is the pool, `query` the link's `?code=` (the group's code the trader was given). */
export async function simPoolView(address, query, page) {
  const at = ethers.getAddress(address);
  const data = await readSimPool(at);
  const given = new URLSearchParams(query || "").get("code") || "";
  const state = data.closed ? `<span class="badge bad">Closed to new sales</span>` : `<span class="badge ok">Open</span>`;
  render(page, `
    <section class="card pool-head">
      <h2>Simulation pool ${esc(chain.short(at))} ${state}</h2>
      <p class="muted">Paper exams: nobody's capital is traded. Pass the exam and then the stage on the same size, and the pool pays
        8% of the size, once for each stage passed, in USDC from the pool's money.</p>
      ${moneyHtml(data.money)}
    </section>
    <section class="card">
      <h3>Menu</h3>
      <label class="sim-code">Your group's code
        <input id="sim-code" type="text" autocomplete="off" spellcheck="false" value="${esc(given)}" placeholder="the code your group gave you">
      </label>
      <div id="sim-menu">${menuHtml(data.rows, given)}</div>
      <p id="sim-status" class="result" role="status"></p>
    </section>
    <section class="card">
      <h3>Terms of this pool</h3>
      ${data.termsText
        ? `<pre class="sim-terms">${esc(data.termsText)}</pre>`
        : `<p class="muted">This pool's terms text could not be read from the chain.</p>`}
      <p class="muted small mono">terms hash ${esc(data.terms)}</p>
    </section>`);

  const status = $("#sim-status", page);
  const input = $("#sim-code", page);
  const repaint = () => {
    $("#sim-menu", page).innerHTML = menuHtml(data.rows, input.value);
  };
  input.addEventListener("input", repaint);
  $("#sim-menu", page).addEventListener("click", async (e) => {
    const button = e.target.closest("button.sim-buy");
    if (!button || button.disabled) return;
    const line = Number(button.dataset.line);
    const all = page.querySelectorAll("button.sim-buy");
    all.forEach((b) => (b.disabled = true));
    status.className = "result busy";
    status.textContent = "Working…";
    try {
      status.textContent = await buy(at, data, line, input.value);
      status.className = "result ok";
    } catch (err) {
      status.className = "result err";
      status.textContent = answer(err);
    } finally {
      repaint();
    }
  });
}

/** The pool's own refusal in words, or the wallet's and the node's as the app words them. */
function answer(err) {
  const iface = new ethers.Interface(SIM_LINES_ABI);
  const name = refusalName(err, iface);
  const args = err?.revert?.args ? [...err.revert.args] : [];
  return poolRefusal(name, args) || friendly(err);
}

/** Buys `line` with the group's code: checks the code against the pool, approves the price if it must, and buys. */
async function buy(address, data, line, codeText) {
  if (!chain.currentSigner()) await chain.connect();
  const signer = chain.currentSigner();
  const pool = new ethers.Contract(address, [...SIM_POOL_ABI, ...SIM_LINES_ABI], signer);
  // Not trimmed inside the library and not folded: a code is its text exactly. Stray spaces at the ends of the input are not part of it.
  const code = codeHash(codeText.trim(), ethers.keccak256);
  const set = await pool.codeOf(code);
  const { args, price } = purchase({ menu: data.rows, line, code, codes: [{ code, partner: set.partner }] });
  const usdc = new ethers.Contract(await pool.usdc(), ERC20_ABI, signer);
  if ((await usdc.allowance(signer.address, address)) < price) await (await usdc.approve(address, price)).wait();
  const receipt = await (await pool.buy(...args)).wait();
  chain.forgetLiveReads();
  if (!receipt || receipt.status !== 1) throw new Error("the purchase reverted");
  return `Bought: the exam is yours (transaction ${receipt.hash}).`;
}
