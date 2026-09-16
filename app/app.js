// Router, wallet button and the entry gate.
import * as chain from "./lib/chain.js";
import { render, $, esc, friendly } from "./lib/ui.js";
import { listView, newPoolView } from "./views/pools.js";
import { poolView } from "./views/pool.js";
import { challengeView } from "./views/challenge.js";
import { verifyView } from "./views/verify.js";

const GATE_KEY = "pools-gate-v1";

function termsView() {
  render(`<section class="card narrow">
    <h2>Terms</h2>
    <p>This is a demo built for the Colosseum Crypto World's Fair. It runs on Hyperliquid testnet and HyperEVM
    testnet with mock USDC that has no value. Nothing here is an offer of investment, trading capital or
    payment.</p>
    <p><strong>Not available to US persons.</strong> Residents and citizens of the United States, and anyone
    acting for them, may not use it. The same applies wherever Hyperliquid itself is not available.</p>
    <p>Contracts, gateway and app are unaudited hackathon code. Keys are held by the Usenami Signer demo
    enclave; the operator runs the gateway that forwards orders.</p>
  </section>`);
}

const routes = [
  [/^#?\/?$/, () => listView()],
  [/^#\/new$/, () => newPoolView()],
  [/^#\/pool\/(0x[0-9a-fA-F]{40})$/, (m) => poolView(m[1])],
  [/^#\/challenge\/(0x[0-9a-fA-F]{40})$/, (m) => challengeView(m[1])],
  [/^#\/verify(?:\/(.*))?$/, (m) => verifyView(m[1] || "")],
  [/^#\/terms$/, () => termsView()],
];

async function route() {
  const hash = location.hash || "#/";
  for (const [pattern, handler] of routes) {
    const m = hash.match(pattern);
    if (m) {
      try {
        await handler(m);
      } catch (err) {
        render(`<section class="card"><h2>Could not load this page</h2><p>${esc(friendly(err))}</p>
          <p class="muted">The public testnet RPC is rate limited; wait a moment and reload.</p></section>`);
      }
      return;
    }
  }
  render(`<section class="card"><h2>Not found</h2><p><a href="#/">Back to the pools</a></p></section>`);
}

function walletButton() {
  const btn = $("#wallet");
  const paint = (addr) => {
    btn.textContent = addr ? chain.short(addr) : "Connect wallet";
  };
  btn.addEventListener("click", async () => {
    try {
      paint(await chain.connect());
      route();
    } catch (err) {
      btn.textContent = "Connect wallet";
      alert(friendly(err));
    }
  });
  chain.onAccount((addr) => {
    paint(addr);
    route();
  });
}

function gate() {
  let passed = false;
  try {
    passed = localStorage.getItem(GATE_KEY) === "ok";
  } catch {
    passed = false;
  }
  if (passed || location.hash === "#/terms") return;
  const dialog = $("#gate");
  const box = $("#gate-us");
  const ok = $("#gate-ok");
  box.addEventListener("change", () => (ok.disabled = !box.checked));
  ok.addEventListener("click", () => {
    try {
      localStorage.setItem(GATE_KEY, "ok");
    } catch {
      // the gate shows again next time; nothing else depends on storage
    }
    dialog.close();
  });
  dialog.addEventListener("cancel", (e) => e.preventDefault());
  dialog.showModal();
}

window.addEventListener("hashchange", route);
walletButton();
gate();
route();
