// "A pool of many traders" (#/new/many): the creation form as a draft, with the model's estimate beside it.
// What is set, its defaults and the estimate's rules are in app/lib/poolform.js; this file only draws them.
// It reads nothing from the chain and sends nothing: the contracts it is for are not deployed.
// The estimate stays in view beside a form longer than the screen (the one inline style here); on a narrow
// screen the two stand one under the other, the form first.
import * as hl from "../lib/hl.js";
import { platformAssets } from "../lib/chain.js";
import { esc, render, $, row } from "../lib/ui.js";
import { FIELDS, INVESTORS, AFTER_TARGET, AT_END, SIZES, MAX_STEPS, TEXT, defaults, outOfBounds, estimate, termWords, scaleWords }
  from "../lib/poolform.js";

const list = (items) => `<ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`;

const TABLE = "data/pool-quote-units-2026-10-13.json";

const numberInput = (f, value) => `<label>${esc(f.label)}
  <input name="${esc(f.id)}" type="number" inputmode="decimal" step="${f.step}" min="${f.min}"${f.max === null ? "" : ` max="${f.max}"`}
    value="${value === null ? "" : esc(value)}"${value === null ? ' placeholder="choose"' : ""}></label>${f.note
  ? `<p class="small">${esc(f.note)}</p>` : ""}`;
const inputs = (section, values) => FIELDS.filter((f) => f.section === section).map((f) => numberInput(f, values[f.id])).join("");
const options = (list, chosen) => list.map((o) =>
  `<option value="${esc(o.id)}"${o.id === chosen ? " selected" : ""}>${esc(o.label)}</option>`).join("");

function stepsHtml(steps) {
  return `<label>Steps <select name="stepCount">${[...Array(MAX_STEPS)].map((_, i) =>
    `<option value="${i + 1}"${i + 1 === steps.length ? " selected" : ""}>${i + 1}</option>`).join("")}</select></label>
    ${steps.map((target, i) => `<label>Target of step ${i + 1}, %
      <input name="step" type="number" inputmode="decimal" step="0.01" min="0.01" max="100" value="${target === null ? "" : esc(target)}"></label>`).join("")}`;
}

/**
 * The form's values as app/lib/poolform.js reads them: a number, or null where nothing is typed or chosen. Read from
 * the fields themselves and not from FormData, which leaves out what stands in a section that is not open yet.
 */
function read(form) {
  const number = (raw) => (raw === null || String(raw).trim() === "" ? null : Number(raw));
  const checked = (name) => [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
  const values = { investors: checked("investors")[0] ?? null, sizes: checked("size").map(Number), assets: checked("asset").map(Number),
    atEnd: form.elements.atEnd.value || null, afterTarget: form.elements.afterTarget.value || null,
    steps: [...form.querySelectorAll('input[name="step"]')].map((input) => number(input.value)) };
  for (const f of FIELDS) values[f.id] = number(form.elements[f.id].value);
  return values;
}

function estimateHtml(result, values) {
  const bounds = outOfBounds(values);
  const beyond = bounds.length
    ? `<p class="small">Outside what the contracts take: ${esc(bounds.map((b) => b.label).join("; "))}.</p>` : "";
  if (!result.ok) {
    // What the form does not take is said as the rule it breaks, not as a missing estimate.
    return `<p><strong>${esc(result.invalid.length ? result.invalid.join(" ") : TEXT.none)}</strong></p>
      ${result.missing.length ? `<p class="small">${esc(TEXT.missing)} ${esc(result.missing.join("; "))}.</p>` : ""}
      ${result.outside.length ? `<p class="small">${esc(TEXT.outside)} ${esc(result.outside.join("; "))}.</p>` : ""}${beyond}`;
  }
  // Two lists and not one table: on a narrow screen a table hides its second column, and the second column here is
  // the one that must not be read as the first.
  const list = (header, pick) => `<h4>${esc(header)}</h4>${result.rows.map((r) => row(r.name, esc(pick(r)))).join("")}`;
  return `${list(result.headers[0], (r) => r.term)}${list(result.headers[1], (r) => r.year)}
    <p class="small">${esc(termWords(result.months))}</p>
    <p class="small muted">${esc(scaleWords(result.usd))}</p>${beyond}`;
}

export async function poolFormView(page) {
  const start = defaults();
  let steps = [...start.steps];
  render(page, `
    <section class="card">
      <h2>${esc(TEXT.title)}</h2>
      <p><strong>${esc(TEXT.draft)}</strong></p>
      <p class="muted">${esc(TEXT.lead)}</p>
    </section>
    <div class="grid">
      <section class="card">
        <form id="many-form">
          <fieldset><legend>1. Who invests</legend>
            ${INVESTORS.map((o) => `<label class="check"><input type="radio" name="investors" value="${esc(o.id)}"${o.built ? "" : " disabled"}>
              ${esc(o.label)}</label>${o.note ? `<p class="small muted">${esc(o.note)}</p>` : ""}`).join("")}
          </fieldset>
          <fieldset class="later" disabled><legend>2. The pool</legend>
            ${inputs("pool", start)}
            <p class="small muted">Sizes on sale, USDC</p>
            <div class="checks">${SIZES.map((s) => `<label class="check"><input type="checkbox" name="size" value="${s}" checked> ${s}</label>`).join("")}</div>
            <label>At the end of the term <select name="atEnd">${options(AT_END, start.atEnd)}</select></label>
            <p class="small muted">${esc(TEXT.atEnd)}</p>
            <p class="small muted">${esc(TEXT.notice)}</p>
          </fieldset>
          <fieldset class="later" disabled><legend>3. The exam</legend>
            <div id="steps">${stepsHtml(steps)}</div>
            ${inputs("exam", start)}
            <p class="small muted">${esc(TEXT.assets)}</p>
            <div class="checks" id="assets"><span class="small muted">Loading…</span></div>
          </fieldset>
          <fieldset class="later" disabled><legend>4. The funded account</legend>
            <p class="small muted">${esc(TEXT.size)}</p>
            ${inputs("funded", start)}
            <label>After the target <select name="afterTarget">${options(AFTER_TARGET, start.afterTarget)}</select></label>
            <p class="small muted">${esc(TEXT.afterTarget)}</p>
          </fieldset>
          <fieldset>
            <label class="check"><input type="checkbox" id="confirm"> ${esc(TEXT.confirmHeading)}</label>
            ${list(TEXT.confirm)}
          </fieldset>
          <p><button type="button" id="send" disabled>${esc(TEXT.send)}</button></p>
          <p class="small muted">${esc(TEXT.unsent)}</p>
          <p class="lead"><strong>${esc(TEXT.last)}</strong></p>
        </form>
      </section>
      <section class="card" style="position: sticky; top: 12px; align-self: start;">
        <h3>${esc(TEXT.estimate)}</h3>
        <p class="muted">${esc(TEXT.label)}</p>
        <div id="estimate"><p class="muted">Loading…</p></div>
        <p class="small" id="footnote">${esc(TEXT.footnote)}</p>
        <p class="small">${esc(TEXT.factorsHeading)}</p>
        <div class="small">${list(TEXT.factors)}</div>
        <p class="small muted">${esc(TEXT.fixed)}</p>
      </section>
    </div>`);

  const form = $("#many-form", page);
  const box = $("#estimate", page);
  let tables = null;
  const show = () => {
    const values = read(form);
    for (const later of form.querySelectorAll("fieldset.later")) later.disabled = values.investors === null;
    if (!tables) return;
    box.innerHTML = estimateHtml(estimate(tables, values), values);
  };
  form.addEventListener("input", (e) => {
    if (e.target.name === "stepCount") {
      // The steps the creator already set stay; a step that is added starts with nothing typed.
      const typed = [...form.querySelectorAll('input[name="step"]')].map((input) => (input.value === "" ? null : Number(input.value)));
      steps = Array.from({ length: Number(e.target.value) }, (_, i) => typed[i] ?? null);
      $("#steps", page).innerHTML = stepsHtml(steps);
    }
    show();
  });
  form.addEventListener("change", show);

  // The table is this site's own file; the names of the assets are Hyperliquid's. Neither is needed to type.
  fetch(TABLE).then((res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }).then((json) => {
    tables = json;
    show();
  }).catch(() => {
    box.innerHTML = `<p><strong>${esc(TEXT.none)}</strong></p><p class="small">The model's table could not be loaded.</p>`;
  });
  const listed = platformAssets();
  hl.perps().catch(() => []).then((perps) => {
    $("#assets", page).innerHTML = listed.length ? listed.map((index) => {
      const perp = perps.find((p) => Number(p.index) === Number(index));
      return `<label class="check"><input type="checkbox" name="asset" value="${esc(index)}"> ${esc(perp ? perp.name : `#${index}`)}</label>`;
    }).join("") : `<span class="small muted">${esc(TEXT.noAssets)}</span>`;
  });
}
