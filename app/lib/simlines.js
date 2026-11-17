// The menu of a simulated pool (src/sim/SimPool.sol) as its two panels show it: the trader's page of the pool and the
// owner's switches. No browser globals and no reads here, so node's test runner loads it (app/tests/simlines.test.mjs):
// a view reads the pool and hands the answers in; these functions say what each line is and what each control may do,
// and write the markup. The pool's contract is the only authority: a control the contract would refuse is shown
// disabled with the reason, and the contract refuses it all the same if a page lets it through.
//
// A line's state is `lineState(line)` and nothing else: (on, sells, canSwitchOn, short). `on` is the owner's switch;
// `sells` is on, the pool open and `COVER_PAYOUTS` payouts of the line free; `canSwitchOn` is the pool open and those
// payouts free; `short` is how much USDC the pool lacks of them. A line never switches itself on (Alex, 16 November):
// the pool being short only stops a line that is on from selling.
import { esc } from "./ui.js";
import { usd } from "./shared.js";
import { menuLines } from "./simpool.js";
import { DRAWDOWN_KINDS } from "./exam.js";

/** What a page reads of a pool beyond SIM_POOL_ABI (app/lib/simpool.js): the lines' state, the pool's free money and
 *  floor, each line's tier, and the pool's refusals of a purchase and of a switch, as the contract declares them. */
export const SIM_LINES_ABI = [
  "function lineState(uint256 line) view returns (bool on, bool sells, bool canSwitchOn, uint256 short)",
  "function free() view returns (uint256)",
  "function floor() view returns (uint256 f)",
  "function tiers() view returns ((uint16 maxDrawdownBps, uint16 targetBps, uint8 drawdownKind)[] k)",
  "error LineOff()",
  "error NotCovered(uint256 short)",
  "error NoSuchCode()",
  "error NoSuchLine()",
  "error Closing()",
  "error NotCreator()",
];

/** What the owner's panel reads and sends beyond SIM_LINES_ABI: the creator's own money in the pool and the switch. */
export const SIM_OWNER_ABI = [
  "function principal() view returns (uint256)",
  "function setLine(uint256 line, bool on)",
  "event LineSet(uint256 indexed line, bool on)",
];

/** The pool's terms are kept as text in the factory's TermsBook, under the hash the pool holds (`terms()`). */
export const TERMS_BOOK_ABI = [
  "function text(bytes32 hash) view returns (string)",
];

const ZERO = /^0x0{40}$/i;

/** A tier in words: the profit target and the loss limit, both a share of the line's size, and how the loss line is
 *  drawn. The numbers are the pool's own (`tiers()`); the words are this page's. */
export function tierOf(tier) {
  const t = { maxDrawdownBps: Number(tier.maxDrawdownBps ?? tier[0]), targetBps: Number(tier.targetBps ?? tier[1]),
    drawdownKind: Number(tier.drawdownKind ?? tier[2]) };
  const pct = (bps) => `${(bps / 100).toFixed(2).replace(/\.?0+$/, "")}%`;
  const kind = (DRAWDOWN_KINDS[t.drawdownKind] ?? `kind ${t.drawdownKind}`).replaceAll("-", " ");
  return {
    key: `${t.targetBps}/${t.maxDrawdownBps}/${t.drawdownKind}`,
    target: t.targetBps, loss: t.maxDrawdownBps, kind: t.drawdownKind,
    short: `${pct(t.targetBps)} / ${pct(t.maxDrawdownBps)}`,
    words: `${pct(t.targetBps)} target, ${pct(t.maxDrawdownBps)} loss limit, ${kind}`,
  };
}

/**
 * The menu, line by line, with each line's tier and its `lineState`. `menu` is `menu()`, `tiers` is `tiers()` in the
 * same order, `states` is `lineState(i)` for each line, in order. A length that does not agree is an error: a page
 * that guessed which tier or state belongs to a line would sell or switch the wrong one.
 */
export function menuRows({ menu, tiers, states }) {
  if (tiers.length !== menu.length || states.length !== menu.length) {
    throw new Error(`the pool's menu has ${menu.length} lines, its tiers ${tiers.length} and its states ${states.length}`);
  }
  return menuLines(menu).map((m, line) => {
    const s = states[line];
    return {
      ...m,
      tier: tierOf(tiers[line]),
      on: Boolean(s.on ?? s[0]), sells: Boolean(s.sells ?? s[1]), canSwitchOn: Boolean(s.canSwitchOn ?? s[2]),
      short: BigInt(s.short ?? s[3]),
    };
  });
}

/** What a trader sees of one line: "sells", "short" (on, the pool lacks the money for two payouts now), "closed" (on,
 *  the pool closed to new sales) or "off" (the owner has it off). Straight from `lineState`. */
export function tradeState(row) {
  if (!row.on) return { kind: "off", words: "Switched off by the pool's owner" };
  if (row.sells) return { kind: "sells", words: "On sale" };
  if (row.short > 0n) {
    return { kind: "short", words: `Not on sale now: the pool does not have the money for two payouts of this line, `
      + `short by ${usd(row.short, 6)} USDC` };
  }
  return { kind: "closed", words: "Not on sale: the pool is closed to new sales" };
}

/** The buy control of a line: active only when the line is on sale and a code has been given (the pool sells only
 *  with a code of its own, `NoSuchCode`); otherwise disabled, and `why` says what is missing. A line that is off has
 *  no control at all. */
export function buyControl(row, code) {
  const state = tradeState(row);
  if (state.kind === "off") return { shown: false, enabled: false, why: state.words };
  if (state.kind !== "sells") return { shown: true, enabled: false, why: state.words };
  if (!String(code ?? "").trim()) return { shown: true, enabled: false, why: "Enter your group's code to buy: this pool sells by a group's code" };
  return { shown: true, enabled: true, why: "" };
}

/** What the owner sees of one line's switch. Off can always be done; on only when `lineState` says `canSwitchOn`, and
 *  otherwise the switch is disabled with the sum the pool lacks. */
export function switchState(row) {
  if (row.on) {
    const selling = row.sells;
    return {
      kind: selling ? "on" : "on-idle", checked: true, enabled: true, next: false,
      note: selling ? "On sale"
        : row.short > 0n ? `On, not selling now: short by ${usd(row.short, 6)} USDC. It sells again by itself once the money is back`
          : "On, not selling now: the pool is closed to new sales",
    };
  }
  if (row.canSwitchOn) return { kind: "off-ready", checked: false, enabled: true, next: true, note: "Off. The pool has the money for two payouts of this line: it can be switched on" };
  if (row.short > 0n) {
    return { kind: "off-short", checked: false, enabled: false, next: true,
      note: `Off. Cannot be switched on: short by ${usd(row.short, 6)} USDC` };
  }
  return { kind: "off-closed", checked: false, enabled: false, next: true, note: "Off. Cannot be switched on: the pool is closed" };
}

/** The menu as a grid: one row for each size, one column for each tier, the lines in their cells. The sizes run up,
 *  the tiers from the lowest target. Whatever order the pool listed its lines in. */
export function menuGrid(rows) {
  const tiers = [];
  for (const r of rows) if (!tiers.some((t) => t.key === r.tier.key)) tiers.push(r.tier);
  tiers.sort((a, b) => a.target - b.target || a.loss - b.loss || a.kind - b.kind);
  const sizes = [...new Set(rows.map((r) => r.size))].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const cells = sizes.map((size) => tiers.map((t) => rows.find((r) => r.size === size && r.tier.key === t.key) || null));
  return { tiers, sizes, cells };
}

/** Who is looking at the owner's panel, from the connected wallet and the pool's `creator()`. */
export function roleOf(account, creator) {
  if (!account) return "no-wallet";
  return String(account).toLowerCase() === String(creator).toLowerCase() ? "owner" : "not-owner";
}

/** The pool's money as a trader sees it: what is free for new sales, the floor the creator cannot take the pool
 *  below, and what is set aside for traders whose payout the token refused. */
export function traderMoney({ free, floor, held }) {
  return [
    { key: "free", label: "Free", value: BigInt(free), note: "What the pool has for new sales: its USDC less what it owes, holds and keeps for stages still running" },
    { key: "floor", label: "Floor", value: BigInt(floor), note: "The creator cannot take the pool's money below this while the pool runs" },
    { key: "held", label: "Set aside for traders", value: BigInt(held), note: "Payouts the token refused to transfer, kept for the traders to claim" },
  ];
}

/** The pool's money as its owner sees it: free, locked (the creator's own reserve), set aside for partners by their
 *  codes (not the creator's money, and not money for payouts), and the floor. */
export function ownerMoney({ free, principal, sharesDue, floor }) {
  return [
    { key: "free", label: "Free", value: BigInt(free), note: "What the pool has for new sales and for switching lines on; the codes' shares are not in it" },
    { key: "locked", label: "Locked", value: BigInt(principal), note: "Your reserve in the pool: what you put in, taken back after the profit above it" },
    { key: "shares", label: "Set aside for partners by their codes", value: BigInt(sharesDue), note: "Accrued shares waiting for the partners to claim them: not yours, and not money for payouts" },
    { key: "floor", label: "Floor", value: BigInt(floor), note: "You cannot withdraw below this while the pool runs" },
  ];
}

/** A purchase's or a switch's refusal by the pool, in words; "" for a refusal this page has no words for. */
export function poolRefusal(name, args = []) {
  switch (name) {
    case "LineOff": return "The pool's owner has this line switched off.";
    case "NotCovered": return `The pool does not have the money for two payouts of this line right now: it is short by ${usd(BigInt(args[0] ?? 0), 6)} USDC.`;
    case "NoSuchCode": return "This code is not one of this pool's: the pool sells only with a code of its own.";
    case "NoSuchLine": return "There is no such line on this pool's menu.";
    case "Closing": return "The pool is closed to new sales.";
    case "NotCreator": return "Only the pool's creator can switch a line.";
    default: return "";
  }
}

/** The pool's terms, as the TermsBook holds them: the text, or null when the pool names none or the book has none. */
export function termsText(hash, text) {
  if (!hash || /^0x0{64}$/i.test(hash)) return null;
  return typeof text === "string" && text.length > 0 ? text : null;
}

/** Whether `address` is a real address and not the zero one. */
export const isSet = (address) => Boolean(address) && !ZERO.test(String(address));

// ── markup ───────────────────────────────────────────────────────────────────────────────────────────────────────

const money = (value) => `${esc(usd(value, 6))} USDC`;

/** The money rows of either panel: a label, the sum and what it is. */
export function moneyHtml(rows) {
  return rows.map((r) => `<div class="kv" data-money="${esc(r.key)}"><span>${esc(r.label)}<small class="muted block">${esc(r.note)}</small></span>`
    + `<span>${money(r.value)}</span></div>`).join("");
}

function cellHtml(row, code) {
  const ctl = buyControl(row, code);
  const state = tradeState(row);
  const price = `<div class="sim-price">${money(row.price)}</div><div class="muted small">pays ${money(row.payout)} per stage passed</div>`;
  if (!ctl.shown) {
    return `<td class="sim-cell off" data-line="${row.line}" data-state="off">${price}<div class="small">${esc(state.words)}</div></td>`;
  }
  const why = ctl.why ? `<div class="small sim-why">${esc(ctl.why)}</div>` : "";
  return `<td class="sim-cell ${esc(state.kind)}" data-line="${row.line}" data-state="${esc(state.kind)}">${price}`
    + `<button type="button" class="sim-buy" data-line="${row.line}"${ctl.enabled ? "" : " disabled"}>Buy exam</button>${why}</td>`;
}

/** The trader's menu: the six sizes by the four tiers, each cell with its price, its payout and the control its state
 *  allows. No control of the pool's management is here: a line that is off is shown, grey, without a button. */
export function menuHtml(rows, code = "") {
  const { tiers, sizes, cells } = menuGrid(rows);
  const head = tiers.map((t) => `<th scope="col" title="${esc(t.words)}">${esc(t.short)}<small class="muted block">target / loss limit</small></th>`).join("");
  const body = sizes.map((size, i) => `<tr><th scope="row">${esc(usd(size, 6))} USDC<small class="muted block">account</small></th>`
    + cells[i].map((r) => (r ? cellHtml(r, code) : `<td class="sim-cell off"></td>`)).join("") + "</tr>").join("");
  return `<div class="sim-scroll"><table class="sim-menu"><thead><tr><th scope="col">Account</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/** One owner's switch for each line, with the line's price and what it says about itself. */
export function switchesHtml(rows) {
  const { tiers, sizes, cells } = menuGrid(rows);
  const head = tiers.map((t) => `<th scope="col" title="${esc(t.words)}">${esc(t.short)}<small class="muted block">target / loss limit</small></th>`).join("");
  const one = (row) => {
    const s = switchState(row);
    return `<td class="sim-cell ${esc(s.kind)}" data-line="${row.line}" data-switch="${esc(s.kind)}">`
      + `<div class="sim-price">${money(row.payout)}<small class="muted block">one payout</small></div>`
      + `<button type="button" class="sim-switch ${s.checked ? "is-on" : ""}" role="switch" aria-checked="${s.checked}" data-line="${row.line}" data-next="${s.next}"`
      + `${s.enabled ? "" : " disabled"} aria-label="Line ${row.line}: ${s.checked ? "on" : "off"}"><span></span></button>`
      + `<div class="small sim-why">${esc(s.note)}</div></td>`;
  };
  const body = sizes.map((size, i) => `<tr><th scope="row">${esc(usd(size, 6))} USDC<small class="muted block">account</small></th>`
    + cells[i].map((r) => (r ? one(r) : `<td class="sim-cell off"></td>`)).join("") + "</tr>").join("");
  return `<div class="sim-scroll"><table class="sim-menu"><thead><tr><th scope="col">Account</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
