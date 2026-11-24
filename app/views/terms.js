// The Terms of Use page of a mainnet site (#/terms): the text the site carries beside index.html
// (app/lib/terms.js, TERMS_FILE), drawn as it is written. A text that did not arrive is the page's failure, with its
// Try again, never an empty page of terms.
import { render } from "../lib/ui.js";
import { TERMS_FILE, termsHtml } from "../lib/terms.js";

export async function mainnetTermsView(page) {
  const res = await fetch(`./${TERMS_FILE}`);
  if (!res.ok) throw new Error(`the Terms of Use could not be read (${res.status})`);
  render(page, `<section class="card narrow terms">${termsHtml(await res.text())}</section>`);
}
