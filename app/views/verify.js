// "Check it yourself": what anyone can read without trusting us, and exactly where our word
// still carries weight.
import { CONFIG } from "../config.js";
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { attestationPayload } from "../lib/cbor.js";
import { esc, render, $, badge, row, isAddress, wire } from "../lib/ui.js";

const { ethers } = window;

export async function verifyView(address, page) {
  if (!isAddress(address)) {
    render(page, `<section class="card narrow"><h2>Check it yourself</h2>
      <p>Open this page from a pool or a challenge, or paste an account address:</p>
      <div class="inline"><input id="addr" placeholder="0x…"><button id="go">Check</button></div></section>`);
    $("#go", page).addEventListener("click", () => { location.hash = `#/verify/${$("#addr", page).value.trim()}`; });
    page.insertAdjacentHTML("beforeend", `<section class="card"><h3>The enclave behind the keys</h3><div id="enclave"></div></section>`);
    enclavePanel(page);
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
      <p class="muted">Everything below is read in your browser from HyperEVM testnet, Hyperliquid's public API,
      the Signer demo box and Base. Where a step still rests on our word, it says so.</p>
    </section>
    <section class="card"><h3>1. Who can trade this account</h3><div id="keys" class="muted">Reading…</div></section>
    <section class="card"><h3>2. Trades against the rules</h3><div id="fills" class="muted">Reading…</div></section>
    <section class="card"><h3>3. The enclave behind the keys</h3><div id="enclave"></div></section>`);
  if (!kind) {
    $("#keys", page).textContent = chain.deployed()
      ? "This address was not created by the factory; there is nothing to check."
      : "The contracts are not deployed yet. The enclave check below works already.";
    $("#fills", page).textContent = "";
    enclavePanel(page);
    return;
  }
  const account = chain.contract(kind, address);
  keysPanel(account, address, page).catch((e) => ($("#keys", page).textContent = String(e)));
  fillsPanel(account, address, page).catch((e) => ($("#fills", page).textContent = String(e)));
  enclavePanel(page);
}

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
    can see the replacement took effect without asking us. What you can't see from here: that a key address was
    minted inside the enclave. The Signer build in use has no outside proof of that.</p>`;
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
    <p class="small muted">This checks trades that reached Hyperliquid. An order the enclave refused never reaches
    the exchange, and neither does one that was signed and not sent, so this page can't say the enclave never
    signed something against the rules. It says what traded.</p>`;
}

function enclavePanel(page) {
  $("#enclave", page).innerHTML = `
    <p>The keys that trade these accounts sit in the Usenami Signer enclave (build <code>pcr0-fbaad62f</code>,
    public repository <code>namixai/signer</code>). Ask the demo box for a fresh attestation document:</p>
    <button id="attest">Ask the Signer demo box</button>
    <div id="attest-out"></div>`;
  wire($("#attest", page), async () => {
    const nonce = ethers.hexlify(crypto.getRandomValues(new Uint8Array(16))).slice(2);
    const res = await fetch(`${CONFIG.signerAttestation}?nonce=${nonce}`);
    const body = await res.json();
    const signed = attestationPayload(body.attestation_doc_b64);
    const registry = new ethers.Contract(CONFIG.pcr0Registry, chain.ABI.pcr0Registry,
      new ethers.JsonRpcProvider(CONFIG.baseRpc, 8453, { staticNetwork: true }));
    const [active, owner] = await registry.isPCR0Active("0x" + signed.pcr0);
    $("#attest-out", page).innerHTML = `
      ${row("PCR0 inside the document", `<span class="mono">${esc(signed.pcr0)}</span>`)}
      ${row("Same as the copy the box sends next to it", signed.pcr0 === body.pcr0_sha384 ? badge("yes", "ok") : badge("no", "bad"))}
      ${row("Your nonce came back inside the document", signed.nonce === nonce ? badge("yes", "ok") : badge("no", "bad"))}
      ${row("Base registry, isPCR0Active", active ? `${badge("active", "ok")} owner <span class="mono">${esc(owner)}</span>` : badge("not active", "bad"))}
      <p class="small muted">Compare that owner with the canonical owner published in the <code>namixai/signer</code> README.
      This page reads the PCR0 from the document, but it does not check AWS's signature on it. To check that, and to
      rebuild the measurement from source, follow <code>docs/VERIFY-SIGNER-YOURSELF.md</code> in that repository.</p>`;
    return "Read.";
  });
}
