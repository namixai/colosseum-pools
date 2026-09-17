// Pieces shared by the challenge page and the funded pool page.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import * as gateway from "../lib/gateway.js";
import { esc, $, wire, row, badge, settle } from "../lib/ui.js";

/** Open orders to cancel and positions outside the pool's assets, for stop and settle calls. */
export async function stopInputs(account, rules) {
  const allowed = new Set(rules.assets.map(Number));
  const [cancels, positions] = await Promise.all([hl.cancelsFor(account), hl.positions(account)]);
  const extra = [...new Set(positions.filter((p) => p.index >= 0 && !allowed.has(p.index)).map((p) => p.index))];
  return { cancels, extra };
}

/** Equity against the rules, as HyperCore reports it, and the contract's own verdict. */
export async function equityPanel(box, contract, account) {
  const [state, base, dayBase, rules, verdict, positions] = await Promise.all([
    hl.account(account), contract.drawdownBase(), contract.dayStartEquity(), contract.rules(),
    contract.violation([]), hl.positions(account),
  ]);
  const equity = Number(state.marginSummary.accountValue);
  const notional = Number(state.marginSummary.totalNtlPos);
  const floor = (Number(base) / 1e6) * (1 - Number(rules.maxDrawdownBps) / 1e4);
  const dayFloor = (Number(dayBase) / 1e6) * (1 - Number(rules.dailyLossBps) / 1e4);
  const lev = equity > 0 ? notional / equity : 0;
  const maxLev = Number(rules.maxLeverageX100) / 100;
  const v = Number(verdict);
  box.innerHTML = `
    ${row("Equity (HyperCore)", `${equity.toFixed(2)} USDC`)}
    ${row("Drawdown floor", `${floor.toFixed(2)} USDC ${equity < floor ? badge("below", "bad") : ""}`)}
    ${row("Today's floor", `${dayFloor.toFixed(2)} USDC ${equity < dayFloor ? badge("below", "bad") : ""}`)}
    ${row("Leverage", `${lev.toFixed(2)}× of ${maxLev.toFixed(2)}× ${lev > maxLev ? badge("above", "bad") : ""}`)}
    ${row("Positions", positions.length ? esc(positions.map((p) => `${p.name} ${p.szi}`).join(", ")) : "none")}
    ${row("Contract's verdict", v ? badge(`breaks: ${chain.BREACH[v]}`, "bad") : badge("inside the rules", "ok"))}
    <p class="muted small">The verdict reads HyperCore through precompiles at the start of the block; the numbers
    above come from Hyperliquid's API and can be a second newer.</p>`;
}

/** The order ticket and the open orders, for the trader of this account. */
export async function tradePanel(box, account, rules) {
  const assets = await Promise.all(rules.assets.map((i) => hl.perp(Number(i))));
  box.innerHTML = `
    <h4>Trade</h4>
    <p class="muted small">Your wallet signs the order's fields. The pool gateway checks on chain that this
    account's key is bound to you, then the enclave signs with that key under its own caps.</p>
    <form id="ticket" class="ticket">
      <select name="asset">${assets.map((a) => `<option value="${a.index}">${esc(a.name)}</option>`).join("")}</select>
      <select name="side"><option value="buy">Buy</option><option value="sell">Sell</option></select>
      <input name="size" type="number" step="any" min="0" placeholder="Size">
      <input name="price" type="number" step="any" min="0" placeholder="Limit price">
      <select name="tif"><option>Gtc</option><option>Ioc</option><option>Alo</option></select>
      <label class="check"><input type="checkbox" name="reduce"> reduce only</label>
      <button type="button" id="send">Sign and send</button>
    </form>
    <pre id="ticket-out" class="out"></pre>
    <h4>Open orders</h4>
    <div id="orders" class="muted">Loading…</div>`;

  wire($("#send", box), async () => {
    const f = new FormData($("#ticket", box));
    const perp = assets.find((a) => a.index === Number(f.get("asset")));
    const size = hl.roundSize(Number(f.get("size")), perp.szDecimals);
    const limitPx = hl.roundPrice(Number(f.get("price")), perp.szDecimals);
    const res = await gateway.placeOrder(chain.currentSigner() || (await chain.connect(), chain.currentSigner()), {
      account,
      asset: perp.index,
      isBuy: f.get("side") === "buy",
      limitPx,
      size,
      reduceOnly: f.get("reduce") === "on",
      tif: f.get("tif"),
    });
    $("#ticket-out", box).textContent = JSON.stringify(res, null, 2);
    setTimeout(() => showOrders(box, account), 2500);
    return res.status === "submitted" ? `Sent: ${size} @ ${limitPx}` : `Not placed: ${res.reason || res.code || res.status}`;
  });
  showOrders(box, account);
}

function showOrders(box, account) {
  return settle(refreshOrders(box, account), $("#orders", box));
}

async function refreshOrders(box, account) {
  const holder = $("#orders", box);
  const [orders, list] = await Promise.all([hl.openOrders(account), hl.perps()]);
  if (!orders.length) {
    holder.textContent = "None.";
    return;
  }
  holder.className = "";
  holder.innerHTML = orders
    .map((o) => `<div class="kv"><span>${esc(o.coin)} ${esc(o.side === "B" ? "buy" : "sell")} ${esc(o.sz)} @ ${esc(o.limitPx)}</span>
      <span><button class="secondary small" data-oid="${esc(o.oid)}" data-coin="${esc(o.coin)}">Cancel</button></span></div>`)
    .join("");
  holder.querySelectorAll("button[data-oid]").forEach((b) =>
    wire(b, async () => {
      const perp = list.find((p) => p.name === b.dataset.coin);
      const signer = chain.currentSigner() || (await chain.connect(), chain.currentSigner());
      const res = await gateway.cancelOrder(signer, { account, asset: perp.index, oid: b.dataset.oid });
      setTimeout(() => showOrders(box, account), 2500);
      return res.status === "submitted" ? "Cancel sent." : `Not cancelled: ${res.reason || res.code || res.status}`;
    }),
  );
}
