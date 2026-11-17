// Small DOM helpers. Everything user-visible that comes from the chain or an API goes
// through esc() or textContent.

/** The zero address, and whether two addresses are the same one (case aside): one answer for every page. */
export const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";
export const sameAddress = (a, b) => Boolean(a && b) && String(a).toLowerCase() === String(b).toLowerCase();

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
  if (batchRefused(err)) {
    return `${RPC} refused this page's reads: it takes at most 20 in one batch, and the page sent more at once.`;
  }
  const limiter = rateLimitedBy(err);
  if (limiter === RPC) {
    // The reads have already waited and asked again for a minute (app/lib/gentle.js), so "a few seconds" is not the
    // advice. The limit is the node's count of requests from one address, and every tab and wallet there shares it.
    return `${limiter} is refusing further requests from this browser right now (rate limited). The page waited and `
      + "asked again for a minute. Other tabs of this site and a wallet extension share the same limit: close what "
      + "you do not need, wait a minute and try again.";
  }
  if (limiter) {
    return `${limiter} is refusing further requests from this browser right now (rate limited). `
      + "Wait a few seconds and try again.";
  }
  const named = err.revert?.name || err.sentAs?.refusal;
  const words = refusalWords(err.sentAs, named);
  if (words) return words;
  const reason = named || err.reason || nodeWords(err) || err.shortMessage || err.message || String(err);
  return String(reason).slice(0, 300);
}

/**
 * The name of the contract's own error a call was refused with, or "" for none. ethers names it on a read;
 * on a write the wallet's estimate brings it as bare data, and the contract's interface reads the name off it.
 */
export function refusalName(err, iface) {
  if (err?.revert?.name) return err.revert.name;
  for (const e of layers(err)) {
    if (typeof e.data !== "string" || !e.data.startsWith("0x")) continue;
    // A layer whose bytes this contract does not declare (or a wallet's bare "0x") names nothing; the
    // contract's own error can still be on a layer under it.
    try {
      const name = iface.parseError(e.data)?.name;
      if (name) return name;
    } catch {
      // not an error this contract declares
    }
  }
  return "";
}

/**
 * A contract's refusal of a call the page sent, in words, or "" where its name is all there is to say.
 * TooEarly is three different waits (self-audit v3, round 1): the start window for `abort`, the deadline
 * for `expire`, and the start's own block for a call that passes or ends an account or a funded stage.
 */
export function refusalWords(sentAs, name) {
  const { kind, method } = sentAs || {};
  if (name === "TooEarly" && kind === "challenge") {
    if (method === "abort") return "Too early: a challenge whose capital has not arrived can be aborted once its start window has passed.";
    if (method === "expire") return "Too early: the challenge's deadline has not passed yet.";
    if (["graduate", "breach", "forfeit"].includes(method)) {
      return "Too early: the challenge started in this same block. Try again in a few seconds.";
    }
  }
  if (name === "TooEarly" && kind === "pool" && ["breach", "stopFunded"].includes(method)) {
    return "Too early: the funded stage opened in this same block. Try again in a few seconds.";
  }
  if (name === "NotAllowed" && kind === "pool" && method === "stopFunded") {
    return "Not allowed: only the pool's owner can stop the funded stage, or its trader where the pool lets them.";
  }
  return "";
}

/** An error and what it wraps, outermost first: ethers wraps what the node said, sometimes twice. */
function layers(err) {
  const out = [];
  for (let e = err, depth = 0; e && depth < 5; e = e.error || e.info?.error || e.cause, depth++) out.push(e);
  return out;
}

/**
 * What the node said when ethers made a failed eth_call a CALL_EXCEPTION without a revert's bytes. ethers' own
 * words there, "missing revert data", name a revert the node may not have sent (an "internal error", say); the
 * node's are kept under them, in `info.error` (ethers 6.17.0). With the bytes, ethers' words are the contract's.
 */
function nodeWords(err) {
  if (err.code !== "CALL_EXCEPTION" || typeof err.data === "string") return "";
  return layers(err).slice(1).map((e) => e.message).find((m) => typeof m === "string" && m) || "";
}

const RPC = "The public HyperEVM RPC";
const HYPERLIQUID = "Hyperliquid's testnet API";

/**
 * Which service answered "too many requests", or "" when none did. Who refused belongs in the
 * sentence: a limit from Hyperliquid said in the RPC's name sends the reader to look at the
 * wrong thing, and both are asked for on the same page.
 *
 * The public RPC answers -32005 "rate limited". ethers wraps that answer, sometimes twice, and
 * the raw wrapper carries a whole JSON-RPC payload: pasting it into the page put what looks
 * like a stack trace in the most honest section of the site. Recognise it and say it in words.
 *
 * Inside a batch the same refusal comes back as one answer with no id, so ethers finds no response
 * for its call and throws BAD_DATA "missing response for request" with the node's answer in `value`
 * (measured on 3 Oct 2026, ethers 6.17.0: the sixth batch of twenty in a row). It is read from
 * `value`, not from the message that happens to quote it.
 */
export function rateLimitedBy(err) {
  for (const e of layers(err)) {
    if (answers(e).some((a) => Number(a?.error?.code) === -32005)) return RPC;
    // app/lib/hl.js puts the HTTP status on its own error; nothing else in this app does.
    if (Number(e.status) === 429) return HYPERLIQUID;
    if (Number(e.code) === -32005) return RPC;
    // The same refusal, said by a web server in front of the node rather than by the node.
    if (Number(e.info?.responseStatus) === 429) return RPC;
    if (/rate limit/i.test(String(e.message || ""))) return RPC;
  }
  return "";
}

export function rateLimited(err) {
  return rateLimitedBy(err) !== "";
}

/**
 * The public RPC refused a batch as too large: -32010 "The batch request was too large", "Exceeded max limit of 20".
 * It answers the whole batch with one error and no ids, so ethers finds no response for its first call and throws
 * BAD_DATA "missing response for request" with the node's answer in `value`. BAD_DATA alone reads as a contract of
 * another version, which this is not: the app asked too much at once. Measured on 1 Oct 2026, ethers 6.17.0.
 */
export function batchRefused(err) {
  for (const e of layers(err)) {
    if (Number(e.code) === -32010) return true;
    if (/batch request was too large|Exceeded max limit of \d+/i.test(String(e.message || ""))) return true;
    if (answers(e).some((a) => Number(a?.error?.code) === -32010)) return true;
  }
  return false;
}

/** What the node answered, as ethers keeps it on a BAD_DATA error: `value`, one answer or a list. */
function answers(e) {
  if (Array.isArray(e.value)) return e.value;
  return e.value && typeof e.value === "object" ? [e.value] : [];
}

/**
 * The line under a page that failed to load: what the failure means for the visitor, said only
 * when the error shows it. It used to say "the public testnet RPC is rate limited" under every
 * failure, the decode error too -- and that one no wait and no reload fixes.
 */
export function failureHint(err) {
  // A rate limit gets no line of its own: friendly() has already named who refused and said when
  // to try again, and 429 is not a status this reads as a service that could not be reached. It is
  // asked first: a limit met inside a batch reaches here as BAD_DATA, and under "wait a few seconds"
  // the page then said the deployment does not match and reloading will not help.
  if (rateLimited(err)) return "";
  const all = layers(err);
  // Before BAD_DATA: a refused batch reaches here as BAD_DATA too, and it is not a mismatch with the deployment.
  if (batchRefused(err)) {
    return "That is a fault in this app asking too much at once, not in the contracts or the deployment. Reloading "
      + "the page usually gets past it.";
  }
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
  // A status decides on its own, because it says whether waiting can help: a 5xx, a request
  // timeout (408) and "too early" (425) can pass, while a 400 or a 404 will answer the same way
  // however many times the page is reloaded, and telling the visitor to reload wastes their time.
  const status = Number(e.status ?? e.info?.responseStatus);
  if (Number.isFinite(status)) return status >= 500 || status === 408 || status === 425;
  // fetch itself: "Failed to fetch" in Chrome, "NetworkError when attempting to fetch resource."
  // in Firefox, "Load failed" in Safari.
  const message = String(e.message || "");
  if (e.name === "TypeError" && /failed to fetch|networkerror|load failed/i.test(message)) return true;
  // ethers, when nothing answered at all or the answer never came.
  return ["NETWORK_ERROR", "TIMEOUT", "SERVER_ERROR"].includes(e.code);
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
