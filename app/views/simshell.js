// What the two panels' pages share: the wallet button of their own headers and the menu's jumps to a section.
import * as chain from "../lib/chain.js";
import { $, friendly } from "../lib/ui.js";
import { panelOf } from "../lib/simpanel.js";
import { revealPartnerTab } from "./simcodes.js";

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
  // The user panel's menu gets a Partner tab for a wallet that is written in a code of this pool, and for no other.
  const user = /^#\/sim\/(0x[0-9a-fA-F]{40})/.exec(location.hash);
  if (user && panelOf(location.hash) === "user") revealPartnerTab(page, user[1], { current: /\/partner$/.test(location.hash) }).catch(() => {});
}
