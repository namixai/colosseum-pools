// Router, wallet button and the entry gate.
import * as chain from "./lib/chain.js";
import { render, $, esc, friendly, view } from "./lib/ui.js";
import { createNavigator, needsGate } from "./lib/nav.js";
import { listView, newPoolView } from "./views/pools.js";
import { poolView } from "./views/pool.js";
import { challengeView } from "./views/challenge.js";
import { verifyView } from "./views/verify.js";

const GATE_KEY = "pools-gate-v1";

function termsView(page) {
  render(page, `<section class="card narrow">
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
  [/^#?\/?$/, (m, page) => listView(page)],
  [/^#\/new$/, (m, page) => newPoolView(page)],
  [/^#\/pool\/(0x[0-9a-fA-F]{40})$/, (m, page) => poolView(m[1], page)],
  [/^#\/challenge\/(0x[0-9a-fA-F]{40})$/, (m, page) => challengeView(m[1], page)],
  [/^#\/verify(?:\/(.*))?$/, (m, page) => verifyView(m[1] || "", page)],
  [/^#\/terms$/, (m, page) => termsView(page)],
];

const navigate = createNavigator(() => {
  const page = document.createElement("div");
  view().replaceChildren(page);
  return page;
});

function failed(page, err) {
  render(page, `<section class="card"><h2>Could not load this page</h2><p>${esc(friendly(err))}</p>
    <p class="muted">The public testnet RPC is rate limited; wait a moment and reload.</p></section>`);
}

function route() {
  const hash = location.hash || "#/";
  if (needsGate(hash, gatePassed())) openGate();
  for (const [pattern, handler] of routes) {
    const m = hash.match(pattern);
    if (m) return navigate((page) => handler(m, page), failed);
  }
  return navigate((page) =>
    render(page, `<section class="card"><h2>Not found</h2><p><a href="#/">Back to the pools</a></p></section>`));
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

// Answered in this tab, even if storage is blocked; then the question comes back next visit.
let answered = false;

function gatePassed() {
  if (answered) return true;
  try {
    return localStorage.getItem(GATE_KEY) === "ok";
  } catch {
    return false;
  }
}

function openGate() {
  const dialog = $("#gate");
  if (!dialog.open) dialog.showModal();
}

function setUpGate() {
  const dialog = $("#gate");
  const box = $("#gate-us");
  const ok = $("#gate-ok");
  box.addEventListener("change", () => (ok.disabled = !box.checked));
  ok.addEventListener("click", () => {
    answered = true;
    try {
      localStorage.setItem(GATE_KEY, "ok");
    } catch {
      // the question comes back on the next visit; nothing else depends on storage
    }
    dialog.close();
  });
  dialog.addEventListener("cancel", (e) => e.preventDefault());
}

window.addEventListener("hashchange", route);
walletButton();
setUpGate();
route();
