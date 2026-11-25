// The Terms of Use of a mainnet site, drawn from their text: app/data/terms-mainnet.md, as Marketing wrote it, is the
// one copy of the words, and this file only turns its few forms into a page. No browser globals and no reads here, so
// node's test runner loads it (app/tests/terms.test.mjs). Every character of the text is escaped: nothing in it is
// taken as markup but the forms below.
import { esc } from "./ui.js";

/** Where the site keeps the text, beside index.html. scripts/pack-site.sh reads the same file: a mainnet site is not
 *  packed while it still holds a "[TO CONFIRM" place. */
export const TERMS_FILE = "data/terms-mainnet.md";

// `**bold**` is the one form inside a line.
const inline = (text) => esc(text).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());

/**
 * The text's blocks as HTML: `# ` the page's heading, `## ` a section's, a run of `- ` or `1. ` lines a list, a run of
 * `|` lines a table whose second line only marks the header, and any other run of lines that are not blank a paragraph,
 * each line's leading and trailing spaces dropped.
 */
export function termsHtml(text) {
  const out = [];
  const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    const run = (re) => {
      const taken = [];
      while (i < lines.length && re.test(lines[i])) taken.push(lines[i++]);
      return taken;
    };
    if (!line.trim()) {
      i += 1;
    } else if (/^# /.test(line)) {
      out.push(`<h2>${inline(line.slice(2))}</h2>`);
      i += 1;
    } else if (/^## /.test(line)) {
      out.push(`<h3>${inline(line.slice(3))}</h3>`);
      i += 1;
    } else if (/^- /.test(line)) {
      out.push(`<ul>${run(/^- /).map((l) => `<li>${inline(l.slice(2))}</li>`).join("")}</ul>`);
    } else if (/^\d+\. /.test(line)) {
      out.push(`<ol>${run(/^\d+\. /).map((l) => `<li>${inline(l.replace(/^\d+\. /, ""))}</li>`).join("")}</ol>`);
    } else if (/^\|/.test(line)) {
      const [head, , ...body] = run(/^\|/);
      out.push(`<div class="scroll"><table><thead><tr>${cells(head).map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`
        + `<tbody>${body.map((row) => `<tr>${cells(row).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    } else {
      // Any other line that is not blank, leading spaces too: one the forms above do not take is always taken here,
      // so the loop moves on from every line (review of #346).
      out.push(`<p>${run(/^(?!# |## |- |\d+\. |\|)\s*\S/).map((l) => inline(l.trim())).join(" ")}</p>`);
    }
  }
  return out.join("\n");
}
