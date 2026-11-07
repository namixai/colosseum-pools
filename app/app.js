// Router, wallet button and the entry gate.
import * as chain from "./lib/chain.js";
import { render, $, esc, friendly, failureHint, view } from "./lib/ui.js";
import { waitWords } from "./lib/gentle.js";
import { createNavigator, needsGate, scrollTarget, LOADING } from "./lib/nav.js";
import { wireWallet } from "./lib/walletbutton.js";
import { BUILD_FILE, buildLink, gatewayLink } from "./lib/build.js";
import { CONFIG } from "./config.js";
import { listView, newPoolView } from "./views/pools.js";
import { poolView } from "./views/pool.js";
import { challengeView } from "./views/challenge.js";
import { verifyView } from "./views/verify.js";
import { economicsView } from "./views/economics.js";
import { sharedView } from "./views/shared.js";
import { evidenceView } from "./views/evidence.js";
import { poolFormView } from "./views/poolform.js";

const GATE_KEY = "pools-gate-v1";

function termsView(page) {
  render(page, `<section class="card narrow">
    <h2>Terms</h2>
    <p>This is a demo built for the Colosseum Crypto World's Fair. It runs on Hyperliquid testnet and HyperEVM
    testnet with mock USDC that has no value. Nothing here is an offer of investment, trading capital or
    payment.</p>
    <p>Contracts, gateway and app are unaudited hackathon code. In this demo the agent keys are testnet
    keys held by the operator's pool gateway, which checks the platform's caps in its own code before it
    signs. Usenami Signer, our enclave signing service, is not used here.</p>
  </section>`);
}

const routes = [
  [/^#?\/?$/, (m, page) => listView(page)],
  [/^#\/new$/, (m, page) => newPoolView(page)],
  // A draft of the form for a pool of many traders. No page links to it: its contracts are not deployed.
  [/^#\/new\/many$/, (m, page) => poolFormView(page)],
  [/^#\/pool\/(0x[0-9a-fA-F]{40})$/, (m, page) => poolView(m[1], page)],
  [/^#\/challenge\/(0x[0-9a-fA-F]{40})$/, (m, page) => challengeView(m[1], page)],
  [/^#\/verify(?:\/(.*))?$/, (m, page) => verifyView(m[1] || "", page)],
  [/^#\/economics$/, (m, page) => economicsView(page)],
  [/^#\/shared(?:\/(0x[0-9a-fA-F]{40}))?$/, (m, page) => sharedView(m[1], page)],
  [/^#\/evidence$/, (m, page) => evidenceView(page)],
  [/^#\/terms$/, (m, page) => termsView(page)],
];

// A new page says it is loading until its view writes into it: the page before it is gone at once, and an empty
// screen says nothing. Leaving a page tells the reader, so what that page still had waiting is not sent.
const navigate = createNavigator(() => {
  const page = document.createElement("div");
  page.innerHTML = LOADING;
  view().replaceChildren(page);
  return page;
}, { leave: chain.leavePage });

// Where each page was left is written into its entry in the browser's history, a moment after the scrolling stops.
// The browser's own restoring is switched off: it runs before a page has its content, and lands short.
let scrollNote = 0;
function keepScroll() {
  try {
    history.scrollRestoration = "manual";
  } catch {
    // an old browser restores as it always did
  }
  const write = () => {
    clearTimeout(scrollNote);
    try {
      history.replaceState({ ...(history.state || {}), y: Math.round(window.scrollY) }, "");
    } catch {
      // history is not writable here: every page opens at its top
    }
  };
  window.addEventListener("scroll", () => {
    clearTimeout(scrollNote);
    scrollNote = setTimeout(write, 80);
  }, { passive: true });
  // A click may be the one that leaves the page, within that moment of the last scroll: the place is written at
  // once, while the entry is still this page's.
  document.addEventListener("click", write, true);
}

/** Opens the page at `y`. A page still loading is shorter than that: go there again once it has loaded, unless the
 *  visitor has moved meanwhile. */
function place(y, shown) {
  window.scrollTo(0, y);
  if (!y) return;
  let moved = false;
  const note = () => {
    moved = true;
  };
  const signs = ["wheel", "touchmove", "keydown", "mousedown"];
  for (const sign of signs) window.addEventListener(sign, note, { once: true, passive: true });
  shown.then((current) => {
    for (const sign of signs) window.removeEventListener(sign, note);
    if (current && !moved) window.scrollTo(0, y);
  });
}

// The cause is said only when the error shows it (failureHint): a decode error is not a busy RPC. By the time a
// rate limit reaches here the reads have already waited and asked again (app/lib/gentle.js); the button asks once more.
function failed(page, err) {
  const hint = failureHint(err);
  render(page, `<section class="card"><h2>Could not load this page</h2><p>${esc(friendly(err))}</p>
    ${hint ? `<p class="muted">${esc(hint)}</p>` : ""}
    <p><button id="try-again">Try again</button></p></section>`);
  $("#try-again", page).addEventListener("click", route);
}

// While the reads wait for the node's limit, say so: the page is not stuck, and it has not failed.
function rpcWaitNotice() {
  const box = $("#rpc-wait");
  if (!box) return;
  chain.onRpcWait((wait) => {
    box.textContent = waitWords(wait);
    box.hidden = !wait;
  });
}

function viewFor(hash) {
  for (const [pattern, handler] of routes) {
    const m = hash.match(pattern);
    if (m) return (page) => handler(m, page);
  }
  return (page) =>
    render(page, `<section class="card"><h2>Not found</h2><p><a href="#/">Back to the pools</a></p></section>`);
}

function route() {
  const hash = location.hash || "#/";
  if (needsGate(hash, gatePassed())) openGate();
  // A note of the page being left must not land in the entry of the page being opened.
  clearTimeout(scrollNote);
  const y = scrollTarget(history.state);
  const shown = navigate(viewFor(hash), failed);
  place(y, shown);
  return shown;
}

function walletButton() {
  const btn = $("#wallet");
  wireWallet({
    button: btn,
    connect: () => chain.connect(),
    onAccount: (fn) => chain.onAccount(fn),
    paint: (addr) => {
      btn.textContent = addr ? chain.short(addr) : "Connect wallet";
    },
    route,
    report: (err) => alert(friendly(err)),
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
  const box = $("#gate-agree");
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

// The commit this copy was packed from, in the footer, when the copy carries one (app/lib/build.js).
function showBuild() {
  const link = $("#build");
  if (!link) return;
  fetch(BUILD_FILE, { cache: "no-store" })
    .then((res) => (res.ok ? res.json() : null))
    .then((build) => {
      const shown = buildLink(build);
      if (!shown) return;
      link.textContent = shown.text;
      link.href = shown.href;
      link.title = shown.title;
      link.hidden = false;
    })
    .catch(() => {
      // no build file, or not JSON: a copy that was not packed says nothing
    });
}

// The commit the pool gateway on the host runs, as its /v1/health names it, beside the site's own.
function showGatewayCommit() {
  const link = $("#gateway-commit");
  if (!link) return;
  fetch(`${CONFIG.gateway}/v1/health`, { cache: "no-store" })
    .then((res) => (res.ok ? res.json() : null))
    .then((health) => {
      const shown = gatewayLink(health);
      if (!shown) return;
      link.textContent = shown.text;
      link.href = shown.href;
      link.title = shown.title;
      link.hidden = false;
    })
    .catch(() => {
      // the gateway did not answer, or named no commit: the footer says nothing about it
    });
}

keepScroll();
showBuild();
showGatewayCommit();
window.addEventListener("hashchange", route);
walletButton();
rpcWaitNotice();
setUpGate();
route();
