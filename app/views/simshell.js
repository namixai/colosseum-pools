// What the two panels' pages share: the wallet button of their own headers and the menu's jumps to a section.
import * as chain from "../lib/chain.js";
import { $, friendly } from "../lib/ui.js";

/** The connected account as the header shows it, or "" when none. */
export const accountLabel = () => (chain.currentAddress() ? chain.short(chain.currentAddress()) : "");

/** Wires the header's wallet button (connecting re-draws the page: app.js listens to the account) and the jumps. */
export function wireShell(page) {
  const wallet = $("#sp-wallet", page);
  if (wallet) wallet.addEventListener("click", () => chain.connect().catch((err) => alert(friendly(err))));
  page.querySelectorAll("a[data-jump]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    const target = page.querySelector(`#${a.dataset.jump}`);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
}
