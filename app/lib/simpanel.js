// The control panel of a simulated pool, apart from the trader's panel (Alex, 17 November: "разделяй админ панель и
// юзер панель"): its sections and the two screens that come before them. No browser globals, so node's test runner
// loads it. A section that is not built yet is in the navigation, disabled, so the owner sees where it will be.
import { esc } from "./ui.js";

/** The panel's sections, in order. `built` is whether this page has it; the rest are the next windows' work. */
export const PANEL_SECTIONS = Object.freeze([
  { key: "lines", label: "Lines and money", built: true },
  { key: "withdraw", label: "Withdraw and finish", built: false },
  { key: "codes", label: "Codes", built: false },
  { key: "operator", label: "Operator", built: false },
]);

/** The panel's own navigation: the sections of the control panel. It does not link to the trader's panel's pages. */
export function panelNavHtml(address, current, sections = PANEL_SECTIONS) {
  const items = sections.map((s) => {
    if (!s.built) return `<span class="panel-tab off" aria-disabled="true" title="Not built yet">${esc(s.label)}</span>`;
    return `<a class="panel-tab${s.key === current ? " on" : ""}" href="#/manage/${esc(address)}"${s.key === current ? ' aria-current="page"' : ""}>${esc(s.label)}</a>`;
  }).join("");
  return `<nav class="panel-nav" aria-label="Control panel">${items}</nav>`;
}

/** What an address that is not the pool's creator sees, and the whole of it. */
export function notOwnerHtml() {
  return `<section class="card narrow"><h2>Control panel</h2>
    <p>This is the pool owner's panel. Your address is not this pool's owner.</p></section>`;
}

/** What a visitor with no wallet connected sees: the panel opens by wallet. */
export function noWalletHtml() {
  return `<section class="card narrow"><h2>Control panel</h2>
    <p>This is the pool owner's panel. Connect the wallet that owns the pool to open it.</p>
    <p><button id="panel-connect" type="button">Connect wallet</button></p></section>`;
}
