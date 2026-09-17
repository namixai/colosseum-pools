// "Check it yourself": what anyone can read without trusting us, and exactly where our word
// still carries weight.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { esc, render, $, badge, row, isAddress } from "../lib/ui.js";

const { ethers } = window;

export async function verifyView(address, page) {
  if (!isAddress(address)) {
    render(page, `<section class="card narrow"><h2>Check it yourself</h2>
      <p>Open this page from a pool or a challenge, or paste an account address:</p>
      <div class="inline"><input id="addr" placeholder="0x…"><button id="go">Check</button></div></section>`);
    $("#go", page).addEventListener("click", () => { location.hash = `#/verify/${$("#addr", page).value.trim()}`; });
    page.insertAdjacentHTML("beforeend", `<section class="card"><h3>Who holds the keys</h3>${KEYS_HELD}</section>`);
    return;
  }
  const [isPool, isChallenge] = chain.deployed()
    ? await Promise.all([chain.factory().isPool(address), chain.factory().isChallenge(address)])
    : [false, false];
  const kind = isPool ? "pool" : isChallenge ? "challenge" : null;
  render(page, `
    <section class="card">
      <h2>Check it yourself</h2>
      <p class="mono">${esc(address)} ${badge(kind || "not ours", kind ? "ok" : "bad")}</p>
      <p class="muted">Everything below is read in your browser from HyperEVM testnet and Hyperliquid's public
      API. Where a step still rests on our word, it says so.</p>
    </section>
    <section class="card"><h3>1. Who can trade this account</h3><div id="keys" class="muted">Reading…</div></section>
    <section class="card"><h3>2. Trades against the rules</h3><div id="fills" class="muted">Reading…</div></section>
    <section class="card"><h3>3. Who holds the keys</h3>${KEYS_HELD}</section>`);
  if (!kind) {
    $("#keys", page).textContent = chain.deployed()
      ? "This address was not created by the factory; there is nothing to check."
      : "The contracts are not deployed yet.";
    $("#fills", page).textContent = "";
    return;
  }
  const account = chain.contract(kind, address);
  keysPanel(account, address, page).catch((e) => ($("#keys", page).textContent = String(e)));
  fillsPanel(account, address, page).catch((e) => ($("#fills", page).textContent = String(e)));
}

const KEYS_HELD = `
  <p>In this demo the keys that trade these accounts are testnet keys held by our pool gateway. Before it
  signs, the gateway's own code checks the platform's caps: the asset list, a size cap per asset and
  400 USDC per order. Nothing on this page can show you that; it rests on our word.</p>
  <p class="small muted">Usenami Signer, our enclave signing service, is a separate product and takes no part
  in this demo. Its code is public in <code>namixai/signer</code>.</p>`;

async function keysPanel(account, address, page) {
  const reg = chain.registry();
  // The key bound now comes straight from the registry; older keys only from event history,
  // which a rate-limited RPC may cut short.
  const [current, boundNow] = await Promise.all([account.agentKey(), reg.keyOf(address)]);
  const boundLog = await chain.history(reg, reg.filters.KeyBound(null, address));
  const cutLog = await chain.history(account, account.filters.AgentCut());
  const cuts = cutLog.events;
  const partial = !(boundLog.complete && cutLog.complete);
  const keys = [...new Set([
    ...boundLog.events.map((ev) => ev.args.key),
    ...(boundNow === ethers.ZeroAddress ? [] : [boundNow]),
  ])];
  const rows = [];
  for (const key of keys) {
    const [binding, role] = await Promise.all([reg.bindingOf(key), hl.info({ type: "userRole", user: key })]);
    const hlSays = role.role === "agent"
      ? (chain.same(role.data.user, address) ? badge("agent of this account", "ok") : badge(`agent of ${chain.short(role.data.user)}`, "bad"))
      : badge(`Hyperliquid: ${role.role}`);
    rows.push(`<div class="kv"><span class="mono">${esc(key)}</span><span>
      ${badge(chain.KEY_STATE[Number(binding.state)], Number(binding.state) === 2 ? "ok" : "")}
      trader <span class="mono">${esc(chain.short(binding.trader))}</span> · ${hlSays}</span></div>`);
  }
  const cutRows = cuts.map((ev) => `<div class="kv"><span>block ${ev.blockNumber}</span>
    <span>replaced ${esc(chain.short(ev.args.oldKey))} with keyless <span class="mono">${esc(chain.short(ev.args.keyless))}</span></span></div>`);
  $("#keys", page).className = "";
  $("#keys", page).innerHTML = `
    ${row("Agent key the contract has approved now", current === ethers.ZeroAddress ? "none" : `<span class="mono">${esc(current)}</span>`)}
    <h4>Keys ever bound to this account (KeyRegistry)</h4>${rows.join("") || "<p>none</p>"}
    <h4>Stops (the agent replaced by an address nobody holds)</h4>${cutRows.join("") || "<p>none</p>"}
    ${partial ? '<p class="small">History is partial: the public RPC limits how far back this page may read.</p>' : ""}
    <p class="small muted">"Hyperliquid:" is Hyperliquid's own answer to <code>userRole</code> for each key, so you
    can see the replacement took effect without asking us. What you can't see from here: who holds each key.
    In this demo our gateway does.</p>`;
}

async function fillsPanel(account, address, page) {
  const [rules, fills, list] = await Promise.all([account.rules(), hl.fills(address), hl.perps()]);
  const allowed = new Set(rules.assets.map(Number));
  const rows = fills.slice(0, 200).map((f) => {
    const p = list.find((x) => x.name === f.coin);
    const ok = p && allowed.has(p.index);
    const notional = Number(f.sz) * Number(f.px);
    return `<tr><td>${esc(new Date(f.time).toISOString().slice(0, 19).replace("T", " "))}</td>
      <td>${esc(f.coin)}</td><td>${esc(f.side === "B" ? "buy" : "sell")}</td><td>${esc(f.sz)}</td><td>${esc(f.px)}</td>
      <td>${esc(notional.toFixed(2))}</td><td>${esc(f.closedPnl)}</td><td>${ok ? badge("allowed", "ok") : badge("not allowed", "bad")}</td></tr>`;
  });
  const bad = fills.filter((f) => { const p = list.find((x) => x.name === f.coin); return !(p && allowed.has(p.index)); }).length;
  const verdict = await account.violation([]);
  $("#fills", page).className = "";
  $("#fills", page).innerHTML = `
    ${row("Fills on this account (Hyperliquid API)", String(fills.length))}
    ${row("Fills in an asset outside the rules", bad ? badge(String(bad), "bad") : badge("0", "ok"))}
    ${row("The contract's verdict right now", Number(verdict) ? badge(chain.BREACH[Number(verdict)], "bad") : badge("inside the rules", "ok"))}
    <div class="scroll"><table><thead><tr><th>time (UTC)</th><th>asset</th><th>side</th><th>size</th><th>price</th>
    <th>notional</th><th>closed PnL</th><th>asset rule</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>
    <p class="small muted">This checks trades that reached Hyperliquid. An order our gateway refused never reaches
    the exchange, and neither does one that was signed and not sent, so this page can't say the gateway never
    signed something against the rules. It says what traded.</p>`;
}
