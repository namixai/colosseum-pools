// "What happened on chain" (#/evidence). The rows are static (app/data/evidence.json, held to the documents
// by app/tests/evidence.test.mjs); the page reads the chain only when a reader presses a button, and then says
// whether the node agrees with the row.
import { readProvider } from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { esc, render, friendly } from "../lib/ui.js";
import { DOCS, TEXT, groups, quoted, appLink, howToCheck, receiptVerdict, fillsVerdict } from "../lib/evidence.js";

// This page's layout only; style.css is shared by every page. A record is a sentence, so cells wrap, and on a
// narrow screen each row becomes a block with its column names written in.
const CSS = `
.evidence td { white-space: normal; vertical-align: top; overflow-wrap: anywhere; }
.evidence table.records { table-layout: fixed; }
.evidence table.records th:nth-child(1) { width: 34%; }
.evidence table.records th:nth-child(2) { width: 21%; }
.evidence table.records th:nth-child(3) { width: 27%; }
.evidence td code { font-size: 12px; overflow-wrap: anywhere; }
.evidence .quote { color: var(--muted); margin: 4px 0 0; }
.evidence ul { margin: 0; padding-left: 1.1em; }
.evidence .result { display: block; margin: 4px 0 0; }
.evidence td button { margin-top: 4px; }
@media (max-width: 760px) {
  .evidence table, .evidence tbody, .evidence tr, .evidence td { display: block; width: 100%; }
  .evidence thead { display: none; }
  .evidence tr { border-bottom: 1px solid var(--line); padding: 8px 0; }
  .evidence td { border: 0; padding: 3px 0; }
  .evidence td[data-label]::before { content: attr(data-label); display: block; color: var(--muted); font-size: 12px; }
}`;

const doc = (path) => `<a href="${esc(DOCS + path)}" target="_blank" rel="noopener">${esc(path)}</a>`;
const quotes = (list) => (list || []).map((q) => `<p class="quote">${esc(quoted(q))}</p>`).join("");

function rowHtml(row, i) {
  const [what, account, tx, how] = TEXT.columns;
  const link = appLink(row);
  const acct = row.account
    ? `${row.kind ? `<span class="badge">${esc(row.kind)}</span> ` : ""}<code>${esc(row.account)}</code>`
      + (link ? `<br><a href="${esc(link)}">${esc(TEXT.open)}</a>` : "")
    : `<span class="muted">${esc(TEXT.noAccount)}</span>`;
  const txCell = row.tx
    ? `<code>${esc(row.tx)}</code>${row.block ? `<br><span class="muted">block ${esc(row.block)}</span>` : ""}`
      + (row.sentBy ? `<br><span class="muted">${esc(row.sentBy)}</span>` : "")
      + `<br><button class="secondary" data-receipt="${i}">${esc(TEXT.receipt)}</button>`
    : row.fills?.length
      ? `${row.fills.map((h) => `<code>${esc(h)}</code>`).join("<br>")}<br><span class="muted">${esc(TEXT.onHyperliquid)}</span>`
        + `<br><button class="secondary" data-fills="${i}">${esc(TEXT.fills)}</button>`
      : `<span class="muted">—</span>`;
  return `<tr>
    <td class="what" data-label="${esc(what)}"><strong>${esc(row.what)}</strong>
      <p class="quote">${esc(quoted(row.record))}</p>${quotes(row.notes)}</td>
    <td data-label="${esc(account)}">${acct}</td>
    <td data-label="${esc(tx)}">${txCell}</td>
    <td data-label="${esc(how)}"><ul>${howToCheck(row).map((w) => `<li>${esc(w)}</li>`).join("")}</ul></td>
  </tr>`;
}

function groupHtml(group, index, before = "") {
  const table = group.rows.length ? `<div class="scroll"><table class="records">
      <thead><tr>${TEXT.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${group.rows.map((r) => rowHtml(r, index.get(r))).join("")}</tbody>
    </table></div>` : "";
  return `<section class="card evidence">
    <h3>${esc(group.section)}</h3>
    <p class="muted">From ${doc(group.doc)}.</p>
    ${before}${table}
  </section>`;
}

function poolsHtml(pools) {
  const cells = (p) => {
    const route = p.open.split(" ")[0];
    return [`<code>${esc(p.pool)}</code>`, esc(p.factory), `<a href="${esc(route)}">${esc(p.open)}</a>`, esc(p.showed)];
  };
  return `<section class="card evidence">
    <h3>${esc(TEXT.pools)}</h3>
    <p class="muted">From ${doc(pools.doc)}.</p>
    <div class="scroll"><table>
      <thead><tr>${TEXT.poolColumns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${pools.rows.map((p) => `<tr>${cells(p).map((c, j) =>
        `<td data-label="${esc(TEXT.poolColumns[j])}">${c}</td>`).join("")}</tr>`).join("")}</tbody>
    </table></div>
  </section>`;
}

export async function evidenceView(page) {
  // A missing file is said as one, not as the JSON error an HTML 404 page would give.
  const res = await fetch("./data/evidence.json");
  if (!res.ok) throw new Error(`The page's records (data/evidence.json) did not load: HTTP ${res.status}.`);
  const data = await res.json();
  const index = new Map(data.rows.map((r, i) => [r, i]));
  const all = groups(data.rows);
  // What was staged is a section of the document that holds records too: its words go in that section's card,
  // above its rows, where the document has them. With no rows of its own it follows the document's other rows.
  const staged = (g) => g.doc === data.staged.doc && g.section === data.staged.section;
  if (!all.some(staged)) {
    all.splice(all.map((g) => g.doc).lastIndexOf(data.staged.doc) + 1, 0, { ...data.staged, rows: [] });
  }
  const card = (g) => groupHtml(g, index, staged(g) ? quotes(data.staged.quotes) : "");
  const first = all.filter((g) => g.doc === data.staged.doc);
  const rest = all.filter((g) => g.doc !== data.staged.doc);
  render(page, `<style>${CSS}</style>
    <section class="card">
      <h2>${esc(TEXT.title)}</h2>
      ${TEXT.intro.map((p) => `<p>${esc(p)}</p>`).join("")}
    </section>
    ${first.map(card).join("")}
    ${poolsHtml(data.pools)}
    ${rest.map(card).join("")}
    <section class="card evidence">
      <h3>${esc(TEXT.limits)}</h3>
      <ul>${data.limits.map((l) => `<li>${esc(quoted(l.quote))} <span class="muted">${doc(l.doc)}</span></li>`).join("")}</ul>
    </section>`);

  for (const button of page.querySelectorAll("button[data-receipt]")) {
    const row = data.rows[Number(button.dataset.receipt)];
    ask(button, async () => receiptVerdict(row, await readProvider.getTransactionReceipt(row.tx)));
  }
  for (const button of page.querySelectorAll("button[data-fills]")) {
    const row = data.rows[Number(button.dataset.fills)];
    ask(button, async () => fillsVerdict(row, await hl.fills(row.account)));
  }
}

// A button that asks the node and shows its answer next to itself. A disagreement is an answer, not a failure:
// it is shown whole, in red, and only a read that did not come back is put through friendly().
function ask(button, read) {
  const out = document.createElement("span");
  out.className = "result";
  button.after(out);
  button.addEventListener("click", async () => {
    button.disabled = true;
    out.className = "result busy";
    out.textContent = TEXT.asking;
    try {
      const verdict = await read();
      out.className = `result ${verdict.ok ? "ok" : "err"}`;
      out.textContent = verdict.text;
    } catch (err) {
      out.className = "result err";
      out.textContent = friendly(err);
    } finally {
      button.disabled = false;
    }
  });
}
