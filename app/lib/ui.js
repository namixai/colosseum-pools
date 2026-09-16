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

export function render(html) {
  view().innerHTML = html;
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
  const reason = err.revert?.name || err.reason || err.shortMessage || err.message || String(err);
  return String(reason).slice(0, 300);
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
