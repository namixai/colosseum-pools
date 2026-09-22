// Small DOM helpers. Everything user-visible that comes from the chain or an API goes
// through esc() or textContent.

export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function view() {
  return document.getElementById("view");
}

export function render(page, html) {
  page.innerHTML = html;
}

/** A panel that loads in the background shows its failure instead of "Loading…" forever. */
export function settle(promise, box) {
  return Promise.resolve(promise).catch((err) => {
    if (box) box.textContent = `Could not load: ${friendly(err)}`;
  });
}

export function $(selector, root = document) {
  return root.querySelector(selector);
}

export function isAddress(text) {
  return /^0x[0-9a-fA-F]{40}$/.test(String(text || ""));
}

/** Runs an async action from a button and reports the outcome next to it. */
export function wire(button, action, { done = "Done.", confirm } = {}) {
  if (!button) return;
  const out = document.createElement("span");
  out.className = "result";
  button.after(out);
  button.addEventListener("click", async () => {
    if (confirm && !window.confirm(confirm)) return;
    button.disabled = true;
    out.className = "result busy";
    out.textContent = "Working…";
    try {
      const message = await action();
      out.className = "result ok";
      out.textContent = message || done;
    } catch (err) {
      out.className = "result err";
      out.textContent = friendly(err);
    } finally {
      button.disabled = false;
    }
  });
}

export function friendly(err) {
  if (!err) return "Something went wrong.";
  if (err.code === "ACTION_REJECTED" || err.code === 4001) return "Cancelled in the wallet.";
  if (rateLimited(err)) {
    return "The public HyperEVM RPC is refusing further reads from this browser right now "
      + "(rate limited). Wait a few seconds and try again.";
  }
  const reason = err.revert?.name || err.reason || err.shortMessage || err.message || String(err);
  return String(reason).slice(0, 300);
}

/** An error and what it wraps, outermost first: ethers wraps what the node said, sometimes twice. */
function layers(err) {
  const out = [];
  for (let e = err, depth = 0; e && depth < 5; e = e.error || e.info?.error || e.cause, depth++) out.push(e);
  return out;
}

/**
 * The public RPC answers -32005 "rate limited". ethers wraps that answer, sometimes twice, and
 * the raw wrapper carries a whole JSON-RPC payload: pasting it into the page put what looks
 * like a stack trace in the most honest section of the site. Recognise it and say it in words.
 */
export function rateLimited(err) {
  for (const e of layers(err)) {
    if (Number(e.code) === -32005) return true;
    // The same refusal, said by the web server in front of a node rather than by the node.
    if (Number(e.info?.responseStatus) === 429) return true;
    if (/rate limit/i.test(String(e.message || ""))) return true;
  }
  return false;
}

/**
 * The line under a page that failed to load: what the failure means for the visitor, said only
 * when the error shows it. It used to say "the public testnet RPC is rate limited" under every
 * failure, the decode error too -- and that one no wait and no reload fixes.
 */
export function failureHint(err) {
  // friendly() has already said it, and when to try again.
  if (rateLimited(err)) return "";
  const all = layers(err);
  // ethers says BAD_DATA when a view's answer does not decode against the app's ABI: a contract
  // of another version, or no contract at the address at all, whose answer is "0x".
  if (all.some((e) => e.code === "BAD_DATA")) {
    return "The contracts at the addresses this app is set up with did not answer in the form this "
      + "version of the app reads: the app and the deployment it points at do not match. Reloading "
      + "will not help.";
  }
  if (all.some(unreachable)) {
    return "A service this page reads could not be reached or answered with an error: the HyperEVM "
      + "testnet RPC, Hyperliquid's testnet API or the pool gateway. Reload in a moment.";
  }
  return "";
}

function unreachable(e) {
  if (["NETWORK_ERROR", "SERVER_ERROR", "TIMEOUT"].includes(e.code)) return true;
  // fetch itself: "Failed to fetch" in Chrome, "NetworkError when attempting to fetch resource."
  // in Firefox, "Load failed" in Safari.
  const message = String(e.message || "");
  if (e.name === "TypeError" && /failed to fetch|networkerror|load failed/i.test(message)) return true;
  // Hyperliquid answered with an HTTP error (app/lib/hl.js).
  return /^Hyperliquid info \S+: HTTP \d+$/.test(message);
}

export function pct(bps) {
  return `${(Number(bps) / 100).toFixed(2).replace(/\.00$/, "")}%`;
}

export function when(seconds) {
  const n = Number(seconds);
  if (!n) return "—";
  return new Date(n * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

export function duration(seconds) {
  const s = Number(seconds);
  if (s % 86400 === 0) return `${s / 86400} d`;
  if (s % 3600 === 0) return `${s / 3600} h`;
  return `${s} s`;
}

export function badge(text, tone = "") {
  return `<span class="badge ${esc(tone)}">${esc(text)}</span>`;
}

export function row(label, value) {
  return `<div class="kv"><span>${esc(label)}</span><span>${value}</span></div>`;
}
