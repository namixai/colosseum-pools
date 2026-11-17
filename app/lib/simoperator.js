// The operator's section of a pool's control panel, as mockup 04 draws it: the payouts waiting in the order they were
// claimed (for the pool's approver), the form that stops one with a reason, the stops with their 14 days, and the
// owner's request to withdraw (for the platform's checker and closer). No browser globals and no reads here, so node's
// test runner loads it (app/tests/simoperator.test.mjs): the view reads the pool and the factory and hands the answers
// in; these functions say who may do what and write the words and the markup.
//
// The contract is the only authority. The role is what the chain says (`approver()` of the pool, `checker()` and
// `closer()` of its factory) and a control is drawn only for the role that may call it; the contract refuses anyone
// else all the same. Before every call the page says in words what the call will do. The payout's review
// (app/lib/operator.js, reviewFor) and the call's calldata are that library's; nothing here does money arithmetic.
import { esc, ZERO_ADDRESS, sameAddress } from "./ui.js";
import { dollars, tierOf, isSet } from "./simlines.js";
import { utc, STOP_RESERVE, stopReasonText } from "./simpool.js";
import {
  awaitingApproval, approveSelectedCall, approveAnywayCall, stopCall, withdrawRequest, PAYOUT_DELAY, PAYOUT_HARD, HOLD_WARNING,
} from "./operator.js";

/** What this page reads and sends beyond the libraries' ABIs (SimPool.sol): the daily limit, the reason of a stop, the
 *  checker's lift and the final stop. */
export const SIM_OPERATOR_ABI = [
  "function payoutsPerDay() view returns (uint8)",
  "function payRoom() view returns (uint256 room)",
  "function nextPayAt() view returns (uint64)",
  "function stopReasonOf(uint256 id) view returns (uint8)",
  "function liftStop(uint256 id)",
  "function finalizeStop(uint256 id)",
  "function accountOf(address account) view returns (uint8 kind, uint8 line, bool done)",
  "error NotApprover()",
  "error NotChecker()",
  "error NotCreator()",
  "error BadPayout()",
  "error BadReason()",
  "error BadPaired()",
  "error ReserveOver()",
  "error ReserveRunning()",
  "error NoRequest()",
  "error Done()",
];

/** The pool's refusals of the operator's calls, in words; "" for one this page has no words for. */
export function operatorRefusal(name) {
  return {
    NotApprover: "Refused: only the pool's approver approves payouts.",
    NotChecker: "Refused: only the platform's checker stops or lifts a payout.",
    NotCreator: "Refused: only the pool's creator or the platform's closer cancels a request.",
    BadPayout: "Refused: that payout is not waiting any more (it was paid, stopped already, or the number is wrong).",
    BadReason: "Refused: the evidence does not fit the reason (check the number of transactions and the links).",
    BadPaired: "Refused: the other account is not an account of this platform's pools, or is the trader's own.",
    ReserveOver: "Refused: the 14 days of this stop are over, so it can no longer be lifted.",
    ReserveRunning: "Refused: the 14 days of this stop are not over yet.",
    NoRequest: "Refused: there is no request to cancel any more.",
    Done: "Refused: the pool is finished.",
  }[name] || "";
}

/** The factory's two keys that act on every pool of it (SimPoolFactory.sol): the checker stops and lifts, the closer may
 *  cancel a creator's request. */
export const SIM_OPERATOR_FACTORY_ABI = [
  "function checker() view returns (address)",
  "function closer() view returns (address)",
];

/** An ABI from several lists, a name once: the first list to name a function or an event wins. `payout(id)` is the one
 *  name that several of the libraries write with different fields: put the widest first. */
export function mergeAbis(...lists) {
  const seen = new Set();
  const out = [];
  for (const item of lists.flat()) {
    const m = /^(function|event|error)\s+(\w+)/.exec(item);
    const key = m ? `${m[1]} ${m[2]}` : item;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

/** How many payouts the page reads at once, and how far below the pool's `head()` it looks for stops still inside their
 *  14 days: the pool's head passes a stopped payout, and a payout stopped lately is below it. */
export const PAYOUT_WINDOW = 400;
export const STOP_LOOKBACK = 300;

/**
 * Which payouts to read, oldest first. The queue is the payouts from the pool's `head()` (the first not settled) on, so
 * the oldest one still waiting is always in it, however many payouts the pool has had; and a few hundred below the head,
 * for the stops that are still inside their 14 days. At most `limit` are read; `unread` says how many later ones are not,
 * and the page asks for more.
 */
export function payoutRange({ count, head, limit = PAYOUT_WINDOW, lookback = STOP_LOOKBACK }) {
  const total = Number(count);
  const first = Math.max(0, Math.min(Number(head), total) - lookback);
  const end = Math.min(total, first + limit);
  return { first, end, ids: Array.from({ length: end - first }, (_, i) => first + i), unread: total - end };
}

// ── who is looking ─────────────────────────────────────────────────

const same = sameAddress;

/**
 * The operator's roles of the connected wallet. `kind` is "no-wallet", "no-role" (a wallet that holds none of the
 * three: it sees one sentence and nothing else) or "operator". Roles: "approver" (the pool's `approver()`), "checker"
 * and "closer" (the factory's). A zero address names nobody.
 */
export function operatorRoles({ account, approver, checker, closer }) {
  if (!account) return { kind: "no-wallet", roles: [] };
  const roles = [];
  if (isSet(approver) && same(account, approver)) roles.push("approver");
  if (isSet(checker) && same(account, checker)) roles.push("checker");
  if (isSet(closer) && same(account, closer)) roles.push("closer");
  return { kind: roles.length ? "operator" : "no-role", roles };
}

const ROLE_WORDS = { approver: "Approver", checker: "Checker", closer: "Closer" };
export const roleWords = (roles) => roles.map((r) => ROLE_WORDS[r]).join(" · ");

/** What each role can do on this page, as the contract gives it. */
export function can(roles) {
  return {
    seeQueue: roles.includes("approver") || roles.includes("checker"),
    approve: roles.includes("approver"),
    stop: roles.includes("checker"),
    lift: roles.includes("checker"),
    seeStops: roles.includes("checker"),
    seeRequests: roles.includes("checker") || roles.includes("closer"),
    cancelRequest: roles.includes("closer"),
  };
}

export const NO_ROLE_WORDS = "Your address has no operator role in this pool.";
export const NO_WALLET_WORDS = "Connect a wallet to open the operator panel.";

// ── the stop form: four reasons, and what each needs ───────────────

/** The reasons the contract knows (SimPool.stopPayout), with the evidence each needs, in the mockup's words. `paired`:
 *  "required" (reasons 1 and 2), "none" (3) or "optional" (4). `trades` is the least number of trade transactions;
 *  `links` says what the links are. */
export const STOP_FORM = Object.freeze([
  { reason: 1, title: "Opposite positions with another account of the platform",
    needs: "the other account · trade transactions (at least 2)", paired: "required", trades: 2, links: "none" },
  { reason: 2, title: "Opposite positions, and the two wallets linked",
    needs: "the other account · trade transactions (at least 2) · transactions that link the wallets",
    paired: "required", trades: 2, links: "transactions" },
  { reason: 3, title: "Result disputed: a flaw of the exam, the oracle or the filler",
    needs: "at least 1 transaction showing the flaw · hash of the published dispute text",
    paired: "none", trades: 1, links: "hash" },
  { reason: 4, title: "Prohibited play: a series of exams traded at the edge of the risk, or one account hedged by another",
    needs: "at least 2 transactions · hash of the published analysis · the other account, if there is one",
    paired: "optional", trades: 2, links: "hash" },
]);

const words = (text) => String(text ?? "").split(/[\s,;]+/).filter(Boolean);

/**
 * The stop form's fields as the contract takes them: the call of `stopCall` (operator.js, which checks it as the pool
 * does) or the problem in words. `fields`: `{ id, reason, paired, trades, links }` as typed; trades and links are
 * lists of 0x-hashes separated by commas or spaces.
 */
export function stopFromForm({ pool, fields, keccak256 }) {
  if (fields.id === undefined || fields.id === null || fields.id === "") return { ok: false, problem: "Choose the payout to stop." };
  const form = STOP_FORM.find((f) => f.reason === Number(fields.reason));
  if (!form) return { ok: false, problem: "Choose one of the four reasons." };
  const paired = String(fields.paired ?? "").trim() || ZERO_ADDRESS;
  try {
    const call = stopCall({ pool, id: fields.id, reason: form.reason, paired, trades: words(fields.trades),
      links: words(fields.links), keccak256 });
    return { ok: true, call };
  } catch (err) {
    const text = String(err.message);
    return { ok: false, problem: `${text.charAt(0).toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? "" : "."}` };
  }
}

// ── the words before each call ─────────────────────────────────────

export const idText = (id) => `#${String(id).padStart(4, "0")}`;

/** What `approvePayouts` will do, for the rows chosen. */
export function approveWords(rows) {
  const list = rows.map((r) => `${idText(r.id)} (${dollars(r.amount)})`).join(", ");
  const early = rows.map((r) => `${idText(r.id)} from ${utc(r.dueAt)}`).join(", ");
  return `Approves ${rows.length === 1 ? "payout" : `${rows.length} payouts`} ${list}. An approved payout is paid from its 72 hours (${early}) `
    + "instead of 7 days after its claim, still at most as many a day as the pool allows. Approving sets neither the amount nor the trader, "
    + "and the checker can still stop it until it is paid.";
}

/** What `stopPayout` will do, for the payout and the reason. */
export function stopWords(row, reason, now) {
  const form = STOP_FORM.find((f) => f.reason === Number(reason));
  const until = utc(Number(now) + STOP_RESERVE);
  return `Writes a stop on payout ${idText(row.id)} (${dollars(row.amount)}) with reason ${form.reason} on chain: ${form.title.toLowerCase()}. `
    + `A payout is stopped once. Its money stays in the pool, set aside, for 14 days: the trader may object, and you may lift the stop until ${until}. `
    + "After that the stop is final.";
}

export const liftWords = (stop) => `Lifts the stop of payout ${idText(stop.id)} (${dollars(stop.amount)}): it is due again and is paid as any other once its time has come. `
  + "A stop that stood 14 days cannot be lifted.";

export const finalizeWords = (stop) => `Makes the stop of payout ${idText(stop.id)} (${dollars(stop.amount)}) final: the 14 days are over, the refusal stands and the money it kept is the pool's again. Anyone may send it.`;

export const cancelWords = (request) => `Cancels the creator's request to withdraw ${dollars(request.amount)}. It only cancels: it never moves the money, and the creator may ask again.`;

// ── what the page shows ────────────────────────────────────────────

/** A line in words: the size and the tier, from `menu()` and `tiers()`. */
export function lineLabel(menu, tiers, line) {
  const m = menu?.[line];
  if (!m) return `line ${line}`;
  const size = BigInt(m.size ?? m[0]);
  const k = size / 1_000_000_000n;
  const name = k >= 1n && size % 1_000_000_000n === 0n ? `${k}K` : dollars(size);
  const tier = tiers?.[line] ? tierOf(tiers[line]).label : "";
  return tier ? `${name} · ${tier}` : name;
}

const plain = (p, id) => ({
  id, account: p.account ?? p[0], trader: p.trader ?? p[1], amount: p.amount ?? p[2], dueAt: p.dueAt ?? p[3],
  state: p.state ?? p[4], wasStopped: p.wasStopped ?? p[5], stoppedAt: p.stoppedAt ?? p[6], approved: p.approved ?? p[7],
});

/** The state of a waiting payout in words, and when it can be paid at the earliest, with and without approval. */
export function queueWords(row, nobody) {
  const noApproval = row.hardAt;
  const status = {
    "in-72-hours": { tone: "wait", text: "Waiting · 72 h running" },
    "awaiting-approval": { tone: "ask", text: "Past 72 h · waiting for the approver" },
    "no-approver": { tone: "wait", text: "No approver set · goes out at the 7 days" },
    approved: { tone: "ok", text: "Approved · 72 h running" },
    "approved-payable": { tone: "ok", text: "Approved · can be paid now" },
    "past-ceiling": { tone: "ok", text: "Past the 7 days · pays by itself" },
    payable: { tone: "ok", text: "Past the 72 hours · can be paid now" },
  }[row.status] ?? { tone: "wait", text: row.status };
  const lines = [];
  if (row.approved) lines.push(`approved: not before ${utc(row.dueAt)}`);
  else {
    if (!nobody) lines.push(`with approval: not before ${utc(row.dueAt)}`);
    if (noApproval !== null) lines.push(`without it: not before ${utc(noApproval)} (7 days)`);
    else lines.push(`not before ${utc(row.dueAt)}`);
  }
  return { ...status, lines, claimed: utc(row.claimedAt) };
}

/** The ids of the stops named by `PayoutStopped` events (`{ name, args: { id } }`), each once, oldest first. */
export function stopIdsFromEvents(events) {
  const ids = new Set();
  for (const e of events) if ((e.name ?? e.eventName) === "PayoutStopped") ids.add(Number(e.args.id));
  return [...ids].sort((a, b) => a - b);
}

/** Which blocks the search for stops has covered, in words. `floor` is the oldest block a stop can still be open in. */
export function stopScanNote({ floor, back, latest, head, done }) {
  if (done) return "Every stop of the last 14 days was looked for in the pool's events.";
  const newer = latest < head ? ` Blocks after ${latest} are not read.` : "";
  return `Stops were looked for in blocks ${back + 1}–${latest}; the 14 days reach back to block ${floor}.${newer}`;
}

/**
 * The operator's model: what the page draws, from what it read.
 * `reads`: `{ now, approver, payouts (the last of payout(id), ids from firstId), firstId, accountLines (account → line),
 * menu, tiers, reasons (id → stopReasonOf), payoutsPerDay, payRoom, nextPayAt, requested, readyAt, reviews,
 * extra (`[{ id, payout }]`: payouts read by id that the window does not hold, the stops found in the events), stopScan
 * (`{ done, note }`) }`. The queue is the payouts from the pool's head; the stops are the stopped ones of the window and
 * of `extra`, whatever their number.
 */
export function operatorModel({ pool, roles, reads, keccak256 }) {
  const now = Number(reads.now);
  const may = can(roles);
  const nobody = !isSet(reads.approver);
  const structs = reads.payouts.map((p, i) => plain(p, reads.firstId + i));
  const inWindow = new Set(structs.map((p) => p.id));
  for (const { id, payout } of reads.extra ?? []) if (!inWindow.has(Number(id))) structs.push(plain(payout, Number(id)));
  const byId = new Map(structs.map((p) => [p.id, p]));
  const lineOf = (account) => lineLabel(reads.menu, reads.tiers, reads.accountLines?.get?.(String(account).toLowerCase()));

  const queue = awaitingApproval({ pool, structs, now, reviews: reads.reviews ?? {}, keccak256, approver: reads.approver })
    .sort((a, b) => a.id - b.id)
    .map((r) => {
      const was = byId.get(r.id)?.wasStopped;
      return { ...r, wasStopped: Boolean(was), line: lineOf(r.account), words: queueWords(r, nobody),
        stoppable: may.stop && !was };
    });

  const stops = structs.filter((p) => Number(p.state) === 2 || Number(p.state) === 5).map((p) => {
    const at = Number(p.stoppedAt || 0);
    const reserveEnds = at ? at + STOP_RESERVE : null;
    const final = Number(p.state) === 5;
    const state = final ? "final" : now < reserveEnds ? "window" : "due-final";
    const reason = Number(reads.reasons?.get?.(p.id) ?? 0);
    return { id: p.id, trader: p.trader, amount: BigInt(p.amount), line: lineOf(p.account), stoppedAt: at, reserveEnds, state,
      reason, reasonText: reason ? stopReasonText(reason) : "", reasonShort: reason ? `Reason ${reason}` : "Reason not read",
      canLift: may.lift && state === "window", canFinalize: may.lift && state === "due-final" };
  }).sort((a, b) => a.id - b.id);

  const request = reads.requested === undefined ? null
    : withdrawRequest({ pool, requested: reads.requested, readyAt: reads.readyAt, now, keccak256 });

  const room = Number(reads.payRoom ?? 0);
  const perDay = Number(reads.payoutsPerDay ?? 0);
  const summary = {
    inQueue: queue.length,
    paidDay: Math.max(0, perDay - room), perDay,
    nextSlot: room > 0 || !Number(reads.nextPayAt) ? "now" : utc(reads.nextPayAt),
    stopsOpen: stops.filter((s) => s.state !== "final").length,
    requests: request && (request.status === "waiting" || request.status === "ready") ? 1 : 0,
  };
  return { now, roles, may, queue, stops, request, summary, perDay, nobody, unread: Number(reads.unread || 0),
    stopScan: reads.stopScan ?? null };
}

/** What a selection of waiting payouts does as one call, or why it cannot be one: the pool's rules (a pool red in the
 *  CFO's count is approved one by one; a payout with no approval to give is left out) are the library's. */
export function selectionCall({ pool, rows, keccak256 }) {
  try {
    return { ok: true, call: approveSelectedCall({ pool, rows, keccak256 }), words: approveWords(rows) };
  } catch (err) {
    return { ok: false, problem: err.message };
  }
}

export { approveAnywayCall, HOLD_WARNING, PAYOUT_DELAY, PAYOUT_HARD };

// ── markup (mockup 04; classes prefixed so-, app/simops.css; the panel's own are sp-) ─────────────────────────────

const BRAND = `<img src="brand/mark-small.svg" alt="" width="36" height="36"><b>Upools</b>`;
const walletHtml = (account) => `<button type="button" class="sp-wallet" id="sp-wallet"><span class="sp-wdot"></span>${esc(account || "Connect wallet")}</button>`;

/** The operator's header: the strip, the band with the roles read from the chain, the menu of the sections this role has. */
export function operatorHeaderHtml({ address, account, state, roles = [] }) {
  const may = can(roles);
  const menu = [
    may.seeQueue && { jump: "queue", label: "Payout checks" },
    may.seeStops && { jump: "stops", label: "Stops" },
    may.seeRequests && { jump: "requests", label: "Requests" },
  ].filter(Boolean);
  const nav = menu.length
    ? `<nav class="sp-nav">${menu.map((m, i) => `<a href="#/manage/${esc(address)}/operator" data-jump="${m.jump}"${i === 0 ? ' class="on"' : ""}>${esc(m.label)}</a>`).join("")}</nav>`
    : "";
  const role = state === "operator" ? roleWords(roles) : state === "no-role" ? "No role in this pool" : "Not connected";
  return `<div class="sp-strip m"><span class="sp-route">#/manage/${esc(address)}/operator</span><span class="sp-pill m">Management panel</span></div>`
    + `<header class="sp-mtop"><div class="sp-brand">${BRAND}<span class="sp-mtag">Management</span></div>${nav}`
    + `<div class="sp-role"><span>role read from contract</span><b>${esc(role)}</b></div>${walletHtml(account)}</header>`
    + `<div class="sp-mnote">Separate route. Not linked from the trader's pages. No buy buttons here. The contract holds the rights; this panel only shows them.</div>`;
}

/** What a wallet without a role, or without a wallet, sees under the header: one sentence. */
export const messageHtml = (state) => `<section class="sp-msg"><h2>Operator panel</h2><p>${esc(state === "no-wallet" ? NO_WALLET_WORDS : NO_ROLE_WORDS)}</p></section>`;

const tile = (label, value, note = "", cls = "") => `<div class="so-tile ${cls}"><dt>${esc(label)}</dt><dd>${esc(value)}</dd>${note ? `<p>${esc(note)}</p>` : ""}</div>`;

export function summaryHtml(s, may) {
  return `<dl class="so-stats">`
    + (may.seeQueue ? tile("Payouts in queue", String(s.inQueue), "", "") : "")
    + (may.seeQueue ? tile("Paid in the last 24 h", `${s.paidDay} of ${s.perDay}`, `at most ${s.perDay} payouts a day per pool`) : "")
    + (may.seeQueue ? tile("Next payout slot", s.nextSlot, "read from nextPayAt()", "hot") : "")
    + (may.seeStops ? tile("Stops open", String(s.stopsOpen)) : "")
    + (may.seeRequests ? tile("Requests to check", String(s.requests)) : "")
    + `</dl>`;
}

const pill = (tone, text) => `<span class="so-pill ${esc(tone)}">${esc(text)}</span>`;

function reviewHtml(r) {
  if (r.review) return `<div class="so-sm">review: ${esc(r.review.verdict ?? r.review.action)}${r.review.reasons ? ` · ${esc([].concat(r.review.reasons).join("; "))}` : ""}</div>`;
  return `<div class="so-sm">review: none loaded</div>`;
}

function queueRowHtml(r, may) {
  const w = r.words;
  const pick = may.approve && r.approvable
    ? `<input type="checkbox" class="so-pick" data-id="${r.id}" aria-label="Select payout ${esc(idText(r.id))}"${r.approveAnyway ? " disabled" : ""}>`
    : "";
  const actions = [];
  if (may.approve && r.approvable) {
    actions.push(r.approveAnyway
      ? `<button type="button" class="so-btn" data-approve="${r.id}" data-anyway="1">Approve anyway</button>`
      : `<button type="button" class="so-btn" data-approve="${r.id}">Approve</button>`);
  }
  if (r.stoppable) actions.push(`<button type="button" class="so-btn sun" data-stop-for="${r.id}">Stop with reason</button>`);
  const approval = r.approved ? "approved" : r.status === "no-approver" ? "nobody approves" : "not yet";
  return `<div class="so-row so-q ${esc(w.tone)}" data-payout="${r.id}" data-status="${esc(r.status)}">`
    + `<div class="so-c id">${pick}<span>${esc(idText(r.id))}</span></div>`
    + `<div class="so-c" data-l="Line">${esc(r.line)}</div>`
    + `<div class="so-c" data-l="Payout"><b>${esc(dollars(r.amount))}</b></div>`
    + `<div class="so-c" data-l="Claimed">${esc(w.claimed)}</div>`
    + `<div class="so-c" data-l="Approval">${esc(approval)}</div>`
    + `<div class="so-c" data-l="Paid no sooner than">${w.lines.map((l) => `<div>${esc(l)}</div>`).join("")}</div>`
    + `<div class="so-c st" data-l="State">${pill(w.tone, w.text)}${reviewHtml(r)}</div>`
    + `<div class="so-c act">${actions.join("")}</div></div>`;
}

/** The queue, in the order the payouts were claimed. The approver ticks payouts and approves them in one call. */
export function queueHtml(model) {
  const { may, queue } = model;
  const bar = may.approve
    ? `<div class="so-bar"><button type="button" class="so-btn" id="so-approve-sel" disabled>Approve selected (0)</button>`
      + `<span class="so-why" id="so-sel-why">Tick the payouts to approve in one signature.</span></div>`
    : `<p class="so-why">You see the queue to choose a payout to stop. Approving is the pool's approver's.</p>`;
  const more = model.unread > 0
    ? `<p class="so-why" id="so-unread">${model.unread} later payout${model.unread === 1 ? " is" : "s are"} not read yet. <button type="button" class="so-btn" id="so-more">Read later payouts</button></p>`
    : "";
  const head = `<div class="so-colhead so-q"><span>#</span><span>Line</span><span>Payout</span><span>Claimed</span><span>Approval</span><span>Paid no sooner than</span><span>State</span><span></span></div>`;
  const body = queue.length ? queue.map((r) => queueRowHtml(r, may)).join("") : `<p class="so-empty">No payout is waiting.</p>`;
  return `<section class="sp-box so-box" id="queue"><div class="sp-bh"><b>Payout checks · queue in claim order</b>`
    + `<span>72 h with approval · 7 days at the latest without it</span></div>${bar}${head}${body}${more}`
    + `<div class="so-do" id="so-do-queue" role="status"></div></section>`;
}

/** The stop form: the four reasons with what each needs, the fields, and what the call will do. */
export function stopFormHtml(model) {
  const radios = STOP_FORM.map((f) => `<label class="so-reason" data-reason="${f.reason}"><input type="radio" name="so-reason" value="${f.reason}">`
    + `<span class="n">${f.reason}</span><span class="t"><b>${esc(f.title)}</b><small>needs: ${esc(f.needs)}</small></span></label>`).join("");
  const options = model.queue.filter((r) => r.stoppable).map((r) => `<option value="${r.id}">${esc(idText(r.id))} · ${esc(dollars(r.amount))}</option>`).join("");
  return `<section class="sp-box so-box so-form" id="stop-form"><div class="sp-bh"><b>Stop a payout</b><span>once per payout · written on chain</span></div>`
    + `<div class="so-body"><label class="so-f">Payout<select id="so-stop-id"><option value="">choose…</option>${options}</select></label>`
    + `<div class="so-reasons">${radios}</div>`
    + `<label class="so-f">Other account<input id="so-paired" type="text" autocomplete="off" spellcheck="false" placeholder="0x…"></label>`
    + `<label class="so-f">Trade transactions<input id="so-trades" type="text" autocomplete="off" spellcheck="false" placeholder="tx hash, tx hash"></label>`
    + `<label class="so-f"><span id="so-links-label">Links</span><input id="so-links" type="text" autocomplete="off" spellcheck="false" placeholder="0x…"></label>`
    + `<div class="so-do" id="so-do-stop" role="status">Choose a payout and a reason.</div>`
    + `<div class="so-bar"><button type="button" class="so-btn dark" id="so-stop-go" disabled>Write stop on chain</button></div></div></section>`;
}

function stopRowHtml(s, may) {
  const left = s.state === "final" ? "final" : s.state === "window" ? `lift until ${utc(s.reserveEnds)}` : `14 days ended ${utc(s.reserveEnds)}`;
  const buttons = [];
  if (may.lift) {
    buttons.push(`<button type="button" class="so-btn" data-lift="${s.id}"${s.canLift ? "" : " disabled"}>Lift stop ${esc(idText(s.id))}</button>`);
    buttons.push(`<button type="button" class="so-btn hatched" data-final="${s.id}"${s.canFinalize ? "" : " disabled"}>Make final${s.state === "window" ? ` · after ${esc(utc(s.reserveEnds))}` : ""}</button>`);
  }
  return `<div class="so-row so-s ${esc(s.state)}" data-stop="${s.id}" data-state="${esc(s.state)}">`
    + `<div class="so-c id"><span>${esc(idText(s.id))}</span></div>`
    + `<div class="so-c" data-l="Line">${esc(s.line)}</div>`
    + `<div class="so-c" data-l="Payout kept"><b>${esc(dollars(s.amount))}</b></div>`
    + `<div class="so-c" data-l="Reason"><span class="so-reasonpill">${esc(s.reasonShort)}</span><div class="so-sm">${esc(s.reasonText)}</div></div>`
    + `<div class="so-c" data-l="Can be lifted until">${esc(left)}${s.state === "final" ? "" : `<div class="so-sm">14 days after the stop</div>`}</div>`
    + `<div class="so-c act">${buttons.join("")}</div></div>`;
}

/** The stops, each with its 14 days, and the lift (the checker's) or the final stop (anyone's, after the 14 days). */
export function stopsHtml(model) {
  const { may, stops } = model;
  const head = `<div class="so-colhead so-s"><span>#</span><span>Line</span><span>Payout kept</span><span>Reason</span><span>Can be lifted until</span><span></span></div>`;
  const body = stops.length ? stops.map((s) => stopRowHtml(s, may)).join("") : `<p class="so-empty">No payout is stopped.</p>`;
  const scan = model.stopScan
    ? `<p class="so-why" id="so-stop-scan">${esc(model.stopScan.note)}${model.stopScan.done ? "" : ` <button type="button" class="so-btn" id="so-stops-more">Read earlier stops</button>`}</p>`
    : "";
  return `<section class="sp-box so-box" id="stops"><div class="sp-bh"><b>Stops</b><span>lift within 14 days · then anyone makes it final</span></div>${head}${body}${scan}`
    + `<div class="so-do" id="so-do-stops" role="status"></div></section>`;
}

/** The creator's request to withdraw: seen by the checker and the closer; only the closer may cancel it. */
export function requestsHtml(model) {
  const { may, request } = model;
  let body;
  if (request === null) body = `<p class="so-empty">No request to withdraw.</p>`;
  else if (request.status === "unread") body = `<p class="so-empty">The request could not be read.</p>`;
  else {
    const state = { waiting: "waiting for its week", ready: "ready: the creator may withdraw", lapsed: "lapsed" }[request.status];
    const cancel = may.cancelRequest && request.calls?.cancel
      ? `<button type="button" class="so-btn" data-cancel="1">Cancel request</button><span class="so-why">closer only cancels; it never moves the money</span>`
      : `<span class="so-why">${request.status === "lapsed" ? "Nothing to cancel." : "View only: the contract lets the closer cancel, not the checker."}</span>`;
    body = `<div class="so-row so-r"><div class="so-c" data-l="Request">creator's request to withdraw</div>`
      + `<div class="so-c" data-l="Amount"><b>${esc(dollars(request.amount))}</b></div>`
      + `<div class="so-c" data-l="Ready from">${esc(utc(request.readyAt))}</div>`
      + `<div class="so-c" data-l="Lapses">${esc(utc(request.lapsesAt))}<div class="so-sm">${esc(state)}</div></div>`
      + `<div class="so-c act">${cancel}</div></div>`;
  }
  return `<section class="sp-box so-box" id="requests"><div class="sp-bh"><b>Requests</b><span>withdrawal requests of the pool's creator</span></div>${body}`
    + `<div class="so-do" id="so-do-requests" role="status"></div></section>`;
}

/** The page under the header, for a wallet with a role: the five tiles and the sections of its role. */
export function operatorHtml(model, { name, role }) {
  const { may } = model;
  const lede = may.stop
    ? "Every payout passes here in the order it was claimed. Until it is paid, the checker may stop it once, with one of four reasons written on chain. The trader has 14 days to object."
    : may.approve
      ? "Every payout passes here in the order it was claimed. Approving one lets it go out at its 72 hours instead of 7 days after its claim."
      : "The creator's request to withdraw waits here for a week. The closer may cancel it.";
  const h1 = may.stop ? `Payout checks <em>&amp; stops</em>` : may.approve ? `Payout <em>approvals</em>` : `Withdrawal <em>requests</em>`;
  return `<section class="sp-hero sp-hd"><div class="sp-kick">Operator panel · ${esc(name)} · Hyperliquid testnet</div>`
    + `<h1>${h1}</h1><p class="sp-lede">${esc(lede)}</p></section>`
    + summaryHtml(model.summary, may)
    + (may.seeQueue ? queueHtml(model) : "")
    + (may.stop || may.seeRequests ? `<div class="so-two${may.stop ? "" : " one"}">${may.stop ? stopFormHtml(model) : ""}<div class="so-stack">${may.seeStops ? stopsHtml(model) : ""}${may.seeRequests ? requestsHtml(model) : ""}</div></div>` : "")
    + `<p id="sim-status" class="sp-status" role="status"></p>`;
}
