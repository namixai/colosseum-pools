// The two panels of a pool of exams, each with a shell of its own (Alex, 17 November: the admin panel and the user
// panel are kept apart; mockups 01 and 02): the user panel (the trader's page of the pool) and the control
// panel (the owner's section). No browser globals, so node's test runner loads it. The site's own header and footer
// are hidden on these pages (app/style.css, body[data-panel]): each panel draws its own, and neither links to the other.
import { esc } from "./ui.js";

/** The panel a route belongs to: "user" for #/sim/<pool>, "manage" for #/manage/<pool>, null for any other page. */
export function panelOf(hash) {
  if (/^#\/sim\/0x[0-9a-fA-F]{40}(?:\/partner)?(?:\?.*)?$/.test(hash)) return "user";
  if (/^#\/manage\/0x[0-9a-fA-F]{40}(?:\/operator)?$/.test(hash)) return "manage";
  return null;
}

/** The user panel's menu. Only the pool page is built; the rest are the next windows' work and are shown, disabled. */
export const USER_NAV = Object.freeze([
  { key: "pool", label: "Pool", built: true },
  { key: "trade", label: "Trade", built: false },
  { key: "exams", label: "My exams", built: false },
  { key: "payouts", label: "My payouts", built: false },
]);

/** The control panel's menu: sections of the owner's page, and the ones still to come. */
export const MANAGE_NAV = Object.freeze([
  { key: "lines", label: "Line switches", built: true, jump: "lines" },
  { key: "money", label: "Pool money", built: true, jump: "money" },
  { key: "codes", label: "Codes & shares", built: false },
  { key: "withdraw", label: "Withdraw & finish", built: false },
]);

const BRAND = `<img src="brand/mark-small.svg" alt="" width="40" height="40"><b>Upools</b>`;

function navHtml(items, current, href) {
  return `<nav class="sp-nav">${items.map((s) => {
    if (!s.built) return `<span class="off" aria-disabled="true" title="Not built yet">${esc(s.label)}</span>`;
    return `<a href="${esc(href)}"${s.jump ? ` data-jump="${esc(s.jump)}"` : ""}${s.key === current ? ' class="on" aria-current="page"' : ""}>${esc(s.label)}</a>`;
  }).join("")}</nav>`;
}

/** What the wallet button says: the address, shortened by the caller, or the call to connect. */
const walletHtml = (account) => `<button type="button" class="sp-wallet" id="sp-wallet"><span class="sp-wdot"></span>${esc(account || "Connect wallet")}</button>`;

/** The user panel's header: the strip that says which panel it is, the brand, the menu and the wallet. */
export function userHeaderHtml({ address, account }) {
  return `<div class="sp-strip"><span class="sp-route">#/sim/${esc(address)}</span><span class="sp-pill u">User panel</span></div>`
    + `<header class="sp-top"><div class="sp-brand">${BRAND}<span class="sp-tag">Testnet app</span></div>`
    + `${navHtml(USER_NAV, "pool", `#/sim/${address}`)}${walletHtml(account)}</header>`;
}

/** The control panel's header: a band of its own, the "Management" tag, the role the contract gives the wallet, the
 *  menu and the wallet; and the note that says it is separate. `role` is "owner", "not-owner" or "no-wallet". */
export function manageHeaderHtml({ address, account, role, poolName }) {
  const roleWords = role === "owner" ? `Pool creator · ${poolName}` : role === "not-owner" ? "No role in this pool" : "Not connected";
  return `<div class="sp-strip m"><span class="sp-route">#/manage/${esc(address)}</span><span class="sp-pill m">Management panel</span></div>`
    + `<header class="sp-mtop"><div class="sp-brand">${BRAND}<span class="sp-mtag">Management</span></div>`
    + `${navHtml(MANAGE_NAV, "lines", `#/manage/${address}`)}`
    + `<div class="sp-role"><span>role read from contract</span><b>${esc(roleWords)}</b></div>${walletHtml(account)}</header>`
    + `<div class="sp-mnote">Separate route. Not linked from the trader's pages. No buy buttons here. The contract holds the rights; this panel only shows them.</div>`;
}

/** The footer both panels share: the testnet badge stays while the page is built for the test network. */
export function footerHtml() {
  return `<div class="sp-foot"><span class="sp-disc">Testnet. Mock USDC. Unaudited.</span>`
    + `<span class="sp-src">Everything on this page is read from the pool's contract.</span>`
    + `<span class="sp-sig"><img src="brand/mark-small.svg" alt="" width="34" height="34">pools.usenami.io</span></div>`;
}

/** What an address that is not the pool's creator sees under the header, and the whole of it. */
export function notOwnerHtml() {
  return `<section class="sp-msg"><h2>Control panel</h2><p>This is the pool owner's panel. Your address is not this pool's owner.</p></section>`;
}

/** What a visitor with no wallet connected sees: the panel opens by wallet. */
export function noWalletHtml() {
  return `<section class="sp-msg"><h2>Control panel</h2><p>This is the pool owner's panel. Connect the wallet that owns the pool to open it.</p></section>`;
}
