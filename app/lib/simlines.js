// The menu of a pool of exams (src/sim/SimPool.sol) as its two panels show it: the trader's page of the pool and the
// owner's switches. No browser globals and no reads here, so node's test runner loads it (app/tests/simlines.test.mjs):
// a view reads the pool and hands the answers in; these functions say what each line is and what each control may do,
// and write the markup of the approved mockups (screens 01 and 02, 17 November). The pool's
// contract is the only authority: a control the contract would refuse is shown disabled with the reason, and the
// contract refuses it all the same if a page lets it through.
//
// A line's state is `lineState(line)` and nothing else: (on, sells, canSwitchOn, short). `on` is the owner's switch;
// `sells` is on, the pool open and `COVER_PAYOUTS` payouts of the line free; `canSwitchOn` is the pool open and those
// payouts free; `short` is how much USDC the pool lacks of them. A line never switches itself on (Alex, 16 November):
// the pool being short only stops a line that is on from selling.
//
// Words: nothing here calls the product a simulation or a paper exam (Alex, 17 November), and the trader's page shows
// no fixed payout sum (his markup of mockup 01). What a line needs is two payouts, as the pool counts it.
import { esc } from "./ui.js";
import { usd } from "./shared.js";
import { payoutOf } from "./simpool.js";

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

/** What the owner's panel reads and sends beyond SIM_LINES_ABI: the money the pool keeps (`reserved` is what it holds
 *  for stops) and the switch of a line. */
export const SIM_OWNER_ABI = [
  "function reserved() view returns (uint256)",
  "function setLine(uint256 line, bool on)",
  "event LineSet(uint256 indexed line, bool on)",
];

/** The pool's terms are kept as text in the factory's TermsBook, under the hash the pool holds (`terms()`). */
export const TERMS_BOOK_ABI = [
  "function text(bytes32 hash) view returns (string)",
];

/** How many payouts of a line the pool must have free for the line to sell or be switched on (SimPool.COVER_PAYOUTS). */
export const COVER_PAYOUTS = 2n;

const ZERO = /^0x0{40}$/i;
const KINDS = ["from start", "trailing to start"];

/** A sum of USDC as the mockups write it: a dollar sign, thousands apart, cents only where there are some. */
export function dollars(value) {
  return `$${usd(value, 6).replace(/\.00$/, "")}`;
}

/** A tier in words: the loss limit and the profit target, both a share of the line's size, and how the loss line is
 *  drawn. The numbers are the pool's own (`tiers()`); the words are this page's. */
export function tierOf(tier) {
  const t = { maxDrawdownBps: Number(tier.maxDrawdownBps ?? tier[0]), targetBps: Number(tier.targetBps ?? tier[1]),
    drawdownKind: Number(tier.drawdownKind ?? tier[2]) };
  const pct = (bps) => `${(bps / 100).toFixed(2).replace(/\.?0+$/, "")}%`;
  const kind = KINDS[t.drawdownKind] ?? `kind ${t.drawdownKind}`;
  return {
    key: `${t.maxDrawdownBps}/${t.targetBps}/${t.drawdownKind}`,
    loss: t.maxDrawdownBps, target: t.targetBps, kind: t.drawdownKind,
    short: `${pct(t.maxDrawdownBps)} / ${pct(t.targetBps)}`,
    label: `loss ${pct(t.maxDrawdownBps)} · target ${pct(t.targetBps)}`,
    words: `${pct(t.maxDrawdownBps)} loss limit, ${pct(t.targetBps)} target, ${kind}`,
  };
}

/**
 * The menu, line by line, with each line's tier and its `lineState`. `menu` is `menu()`, `tiers` is `tiers()` in the
 * same order, `states` is `lineState(i)` for each line, in order. A length that does not agree is an error: a page
 * that guessed which tier or state belongs to a line would sell or switch the wrong one. `need` is the two payouts
 * the pool must have free for the line.
 */
export function menuRows({ menu, tiers, states }) {
  if (tiers.length !== menu.length || states.length !== menu.length) {
    throw new Error(`the pool's menu has ${menu.length} lines, its tiers ${tiers.length} and its states ${states.length}`);
  }
  return [...menu].map((m, line) => {
    const size = BigInt(m.size ?? m[0]);
    const s = states[line];
    return {
      line, size, price: BigInt(m.price ?? m[1]), payout: payoutOf(size), need: payoutOf(size) * COVER_PAYOUTS,
      tier: tierOf(tiers[line]),
      on: Boolean(s.on ?? s[0]), sells: Boolean(s.sells ?? s[1]), canSwitchOn: Boolean(s.canSwitchOn ?? s[2]),
      short: BigInt(s.short ?? s[3]),
    };
  });
}

/** What a trader sees of one line: "sells", "short" (on, the pool lacks the money for two payouts now), "closed" (on,
 *  the pool closed to new sales) or "off" (the owner has it off). Straight from `lineState`. */
export function tradeState(row) {
  if (!row.on) return { kind: "off" };
  if (row.sells) return { kind: "sells" };
  return { kind: row.short > 0n ? "short" : "closed" };
}

/** The buy control of a line, as the mockup draws it: "Buy $20" while on sale, "Short $400" when the pool lacks the
 *  money, nothing at all for a line that is off. It is active only on sale and with a code given (the pool sells only
 *  with a code of its own, `NoSuchCode`); `why` says what is missing. */
export function buyControl(row, code) {
  const state = tradeState(row);
  if (state.kind === "off") return { shown: false, enabled: false, label: "Off", why: "Switched off by the pool owner" };
  if (state.kind === "short") return { shown: true, enabled: false, label: `Short ${dollars(row.short)}`, why: `The pool is short ${dollars(row.short)}` };
  if (state.kind === "closed") return { shown: true, enabled: false, label: "Closed", why: "The pool is closed to new sales" };
  const label = `Buy ${dollars(row.price)}`;
  if (!String(code ?? "").trim()) return { shown: true, enabled: false, label, why: "Enter your group's code to buy" };
  return { shown: true, enabled: true, label, why: "" };
}

/** What the owner sees of one line's switch. Off can always be done; on only when `lineState` says `canSwitchOn`, and
 *  otherwise the switch is disabled with the sum the pool lacks. */
export function switchState(row) {
  if (row.on) {
    return {
      kind: row.sells ? "on" : "on-idle", checked: true, enabled: true, next: false,
      note: row.sells ? "On sale"
        : row.short > 0n ? `On, not selling now: short by ${dollars(row.short)}. It sells again by itself once the money is back`
          : "On, not selling now: the pool is closed to new sales",
    };
  }
  if (row.canSwitchOn) return { kind: "off-ready", checked: false, enabled: true, next: true, note: "Off. The pool has the money for two payouts of this line: it can be switched on" };
  if (row.short > 0n) return { kind: "off-short", checked: false, enabled: false, next: true, note: `Off. Cannot be switched on: short by ${dollars(row.short)}` };
  return { kind: "off-closed", checked: false, enabled: false, next: true, note: "Off. Cannot be switched on: the pool is closed" };
}

/** The menu as groups: one for each size, smallest first, its lines by tier (the lowest loss limit first). Whatever
 *  order the pool listed its lines in. `tiers` is the columns. A size lacking a tier has a null in its place. */
export function sizeGroups(rows) {
  const tiers = [];
  for (const r of rows) if (!tiers.some((t) => t.key === r.tier.key)) tiers.push(r.tier);
  tiers.sort((a, b) => a.loss - b.loss || a.target - b.target || a.kind - b.kind);
  const sizes = [...new Set(rows.map((r) => r.size))].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  return {
    tiers,
    groups: sizes.map((size) => {
      const lines = tiers.map((t) => rows.find((r) => r.size === size && r.tier.key === t.key) || null);
      const present = lines.filter(Boolean);
      const first = present[0];
      return { size, need: first.need, payout: first.payout, lines, allOff: present.every((r) => !r.on) };
    }),
  };
}

/** The notes under a size on the trader's page, one for each state its lines are in, in the mockup's words. `free` is
 *  the pool's `free()`. */
export function sizeNotes(group, free) {
  const kinds = new Set(group.lines.filter(Boolean).map((r) => tradeState(r).kind));
  const notes = [];
  if (kinds.has("sells")) {
    notes.push({ kind: "sells", term: "", text: `On sale. The pool has ${dollars(free)} free; this size needs ${dollars(group.need)} (two payouts).` });
  }
  if (kinds.has("short")) {
    const short = group.lines.filter(Boolean).find((r) => tradeState(r).kind === "short").short;
    notes.push({ kind: "short", term: "chart term: shoal",
      text: `On, but the pool is short of money. It has ${dollars(free)} free and needs ${dollars(group.need)} for two payouts, ${dollars(short)} short. Buying comes back by itself when money returns.` });
  }
  if (kinds.has("closed")) notes.push({ kind: "closed", term: "", text: "On, but the pool is closed to new sales." });
  if (kinds.has("off")) notes.push({ kind: "off", term: "chart term: closed channel", text: "Switched off by the pool owner. Not for sale." });
  return notes;
}

/** What the owner's status column says of a size: a label for each state its lines are in, and what the pool has
 *  against what the size needs. */
export function sizeStatus(group, free) {
  const lines = group.lines.filter(Boolean);
  const states = lines.map(switchState);
  const order = ["on", "on-idle", "off-ready", "off-short", "off-closed"];
  const labels = { on: "On · selling", "on-idle": "On · not selling", "off-ready": "Off · can switch on",
    "off-short": "Off · can't switch on", "off-closed": "Off · pool closed" };
  const kinds = order.filter((k) => states.some((s) => s.kind === k));
  const mixed = kinds.length > 1;
  const short = lines.reduce((m, r) => (r.short > m ? r.short : m), 0n);
  const why = [];
  if (kinds.includes("on")) why.push(`free ${dollars(free)} ≥ needs ${dollars(group.need)}`);
  if (kinds.includes("on-idle")) why.push(short > 0n ? `pool short ${dollars(short)}. Sells again by itself when money returns. You can switch it off.` : "The pool is closed to new sales. You can switch it off.");
  if (kinds.includes("off-ready")) why.push(`free ${dollars(free)} ≥ needs ${dollars(group.need)}: it can be switched on.`);
  if (kinds.includes("off-short")) why.push(`needs ${dollars(group.need)} free for two payouts. Short ${dollars(short)}.`);
  if (kinds.includes("off-closed") && !kinds.includes("off-short")) why.push("The pool is closed, so no line can be switched on.");
  return {
    kinds, short,
    badges: kinds.map((k) => ({ kind: k, label: mixed ? `${labels[k]} ×${states.filter((s) => s.kind === k).length}` : labels[k] })),
    why,
  };
}

/** Who is looking at the owner's panel, from the connected wallet and the pool's `creator()`. */
export function roleOf(account, creator) {
  if (!account) return "no-wallet";
  return String(account).toLowerCase() === String(creator).toLowerCase() ? "owner" : "not-owner";
}

/**
 * The pool's money as its owner reads it, in the mockup's four parts. Free is `free()`. Locked is what the pool
 * keeps that is not partners' and not free: payouts owed (`owed`), kept for stops (`reserved`), held for traders
 * (`held`), and for passed exams and running stages (`staged`). Reserved for partners is `sharesDue`. The floor is
 * `floor()`. The bar's marker is where a withdrawal must stop: everything the pool keeps (`owed + held + reserved +
 * sharesDue`) and the floor, the sum SimPool.withdrawable leaves in. `parts` are shares of the bar, in percent.
 */
export function ownerMoney({ free, owed, reserved, held, staged, sharesDue, floor }) {
  const f = BigInt(free);
  const locked = BigInt(owed) + BigInt(reserved) + BigInt(held) + BigInt(staged);
  const shares = BigInt(sharesDue);
  const total = f + locked + shares;
  const share = (v) => (total === 0n ? 0 : Number((v * 10_000n) / total) / 100);
  const marker = BigInt(owed) + BigInt(held) + BigInt(reserved) + shares + BigInt(floor);
  return {
    total, free: f, locked, shares, floor: BigInt(floor),
    breakdown: { owed: BigInt(owed), stops: BigInt(reserved), held: BigInt(held), staged: BigInt(staged) },
    parts: { locked: share(locked), shares: share(shares), free: share(f), floor: total === 0n ? 0 : Math.min(100, share(marker)) },
  };
}

/** A purchase's or a switch's refusal by the pool, in words; "" for a refusal this page has no words for. */
export function poolRefusal(name, args = []) {
  switch (name) {
    case "LineOff": return "The pool's owner has this line switched off.";
    case "NotCovered": return `The pool does not have the money for two payouts of this line right now: it is short by ${dollars(BigInt(args[0] ?? 0))}.`;
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

// ── markup (the mockups' classes, prefixed sp-: app/style.css) ───────────────────────────────────────────────────

const sizeName = (size) => {
  const k = size / 1_000_000_000n;
  return k >= 1n && size % 1_000_000_000n === 0n ? `${k}<small>K</small>` : esc(dollars(size));
};

function noteHtml(n) {
  const buoy = { sells: "g", short: "y", closed: "y", off: "x" }[n.kind];
  const [lead, ...rest] = n.text.split(/(?<=[.!?]) /);
  return `<div class="sp-note ${esc(n.kind)}" data-note="${esc(n.kind)}"><span class="sp-buoy ${buoy}"></span><b>${esc(lead)}</b> ${esc(rest.join(" "))}`
    + `${n.term ? ` <span class="sp-term">${esc(n.term)}</span>` : ""}</div>`;
}

function cellHtml(row, code) {
  const ctl = buyControl(row, code);
  const state = tradeState(row);
  const head = `<div class="sp-tlab">${esc(row.tier.label)}</div><div class="sp-price">${esc(dollars(row.price))}</div>`;
  if (!ctl.shown) {
    return `<div class="sp-cell off" data-line="${row.line}" data-state="off">${head}<span class="sp-offtxt">Off</span></div>`;
  }
  return `<div class="sp-cell ${esc(state.kind)}" data-line="${row.line}" data-state="${esc(state.kind)}">${head}`
    + `<button type="button" class="sp-buy" data-line="${row.line}"${ctl.enabled ? "" : " disabled"}${ctl.why ? ` title="${esc(ctl.why)}"` : ""}>${esc(ctl.label)}</button></div>`;
}

/** The trader's menu, as mockup 01 draws it: a block for each size with its four lines and its note. No payout sum,
 *  no control of the pool's management; a line that is off is shown, struck through, with no button. */
export function menuHtml(rows, free, code = "") {
  const { tiers, groups } = sizeGroups(rows);
  const head = tiers.map((t) => `<span>${esc(t.label)}</span>`).join("");
  const body = groups.map((g) => {
    const kind = g.allOff ? "off" : g.lines.some((r) => r && tradeState(r).kind === "short") ? "short" : "sale";
    return `<section class="sp-size ${kind}" data-size="${esc(g.size.toString())}">`
      + `<div class="sp-sizehead"><div class="sp-depth">${sizeName(g.size)}</div><div class="sp-acct">account ${esc(dollars(g.size))}</div></div>`
      + `<div class="sp-cells">${g.lines.map((r) => (r ? cellHtml(r, code) : `<div class="sp-cell off"></div>`)).join("")}</div>`
      + `<div class="sp-notes">${sizeNotes(g, free).map(noteHtml).join("")}</div></section>`;
  }).join("");
  return `<div class="sp-box sp-menu"><div class="sp-bh"><span>Menu · ${groups.length} sizes × ${tiers.length} tiers · ${rows.length} lines</span>`
    + `<span>loss = loss limit, target = goal, % of the account · 0.5% of each price goes to the platform</span></div>`
    + `<div class="sp-colhead"><span>Account</span>${head}</div>${body}</div>`;
}

/** The owner's money: the bar of its four parts with the floor marked, and the four figures under it. */
export function moneyHtml(m) {
  const b = m.breakdown;
  return `<div class="sp-box sp-money" id="money"><div class="sp-bh"><b>Pool money · ${esc(dollars(m.total))} in the pool</b><span>four parts, read from the contract</span></div>`
    + `<div class="sp-xs"><div class="sp-layer lk" style="width:${m.parts.locked}%"><span>locked</span></div>`
    + `<div class="sp-layer pt" style="width:${m.parts.shares}%"></div>`
    + `<div class="sp-layer fr" style="width:${m.parts.free}%"><span>free</span></div>`
    + `<div class="sp-floor" style="left:${m.parts.floor}%"><span>floor ${esc(dollars(m.floor))} · stays in while open</span></div></div>`
    + `<dl class="sp-four">`
    + `<div class="fr" data-money="free"><dt><span class="sp-sq fr"></span>Free</dt><dd>${esc(dollars(m.free))}</dd><p>Covers new sales and switching lines on. Every line needs two of its payouts here.</p></div>`
    + `<div data-money="locked"><dt><span class="sp-sq lk"></span>Locked</dt><dd>${esc(dollars(m.locked))}</dd><p>Payouts owed ${esc(dollars(b.owed))}, kept for a stop ${esc(dollars(b.stops))}, held for traders ${esc(dollars(b.held))}, passed exams and running stages ${esc(dollars(b.staged))}.</p></div>`
    + `<div data-money="shares"><dt><span class="sp-sq pt"></span>Reserved for partners</dt><dd>${esc(dollars(m.shares))}</dd><p>Accrued to codes and waiting for claim (sharesDue). Not yours and not for payouts.</p></div>`
    + `<div data-money="floor"><dt><span class="sp-sq fl"></span>Floor</dt><dd>${esc(dollars(m.floor))}</dd><p>Must stay in while the pool is open: 3 payouts of the largest line live or selling.</p></div>`
    + `</dl></div>`;
}

function switchCell(row) {
  const s = switchState(row);
  return `<div class="sp-swc ${esc(s.kind)}" data-line="${row.line}" data-switch="${esc(s.kind)}" title="${esc(s.note)}">`
    + `<button type="button" class="sp-tg ${s.checked ? "on" : ""}${s.enabled ? "" : " dis"}" role="switch" aria-checked="${s.checked}" data-line="${row.line}" data-next="${s.next}"`
    + `${s.enabled ? "" : " disabled"} aria-label="Line ${row.line}, ${esc(row.tier.label)}: ${s.checked ? "on" : "off"}"><i></i></button>`
    + `<div><div class="sp-tl">${esc(row.tier.short)}</div><div class="sp-tp">${esc(dollars(row.price))}</div></div></div>`;
}

/** The owner's 24 switches, as mockup 02 draws them: a row for each size with its payout and what it needs, a switch
 *  for each line, and the size's status with the sum the pool lacks beside a switch that cannot be turned on. */
export function switchesHtml(rows, free) {
  const { tiers, groups } = sizeGroups(rows);
  const head = tiers.map((t) => `<span>${esc(t.label)}</span>`).join("");
  const body = groups.map((g) => {
    const st = sizeStatus(g, free);
    return `<div class="sp-swrow ${st.short > 0n && st.kinds.includes("on-idle") ? "short" : ""}" data-size="${esc(g.size.toString())}">`
      + `<div class="sp-swsize"><div class="sp-sz">${sizeName(g.size)}</div><div class="sp-sm">payout ${esc(dollars(g.payout))}<br>needs ${esc(dollars(g.need))}</div></div>`
      + g.lines.map((r) => (r ? switchCell(r) : `<div class="sp-swc"></div>`)).join("")
      + `<div class="sp-st">${st.badges.map((b) => `<span class="sp-state ${esc(b.kind)}">${esc(b.label)}</span>`).join(" ")}`
      + `${st.why.map((w) => `<div class="sp-why">${esc(w)}</div>`).join("")}</div></div>`;
  }).join("");
  return `<div class="sp-box sp-lines" id="lines"><div class="sp-bh"><b>Line switches · ${rows.length} lines</b>`
    + `<span>off any time · on only with two payouts free · a switch acts on new sales only</span></div>`
    + `<div class="sp-swhead"><span>Size</span>${head}<span>Status</span></div>${body}</div>`;
}
