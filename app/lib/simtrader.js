// The trader's side of a pool of exams, beyond its menu: the wallet's exams and stages in the pool, the payouts the pool
// holds for the wallet, and the two calls that move them on (start the stage, request the payout). No browser globals and
// no reads here, so node's test runner loads it (app/tests/simtrader.test.mjs): a view reads the pool and the accounts
// and hands the answers in; these functions say what each row is, what the wallet may press, and write the markup (the
// classes of the approved mockups 01 and 03, prefixed sp-: app/style.css).
//
// Where each fact comes from: an account's kind, line and whether it is done are the pool's `accountOf`; how it stands
// (running, passed, breached, expired) is the account's own `result()`; the payouts are the pool's `payout(id)` for each
// id, kept for the wallet by their `trader`. The pool keeps no list of a wallet's accounts, only the events that sold and
// started them (ExamSold, Funded), so those are looked through in blocks, a stretch at a time, and a page says how far.
//
// Money: nothing is added up or worked out here. A sum is shown only for a payout the pool already holds (requested, so
// the contract wrote its amount, or paid); an exam or a stage that has not been requested has no sum, and the menu and
// the trading screen have none either.
import { esc } from "./ui.js";
import { dollars, tierOf } from "./simlines.js";
import { payouts, soldExams, stages, stopReasonText, utc, PAYOUT_DELAY, PAYOUT_HARD, STOP_RESERVE } from "./simpool.js";
import { STATUS } from "./exam.js";

/** What a page reads of an account (an exam or a stage: both are PaperExam clones), beyond PAPER_EXAM_ABI. */
export const ACCOUNT_ABI = [
  "function pool() view returns (address)",
  "function result() view returns (uint8 status, address trader, uint64 capital, uint64 passedAt)",
];

/** What the trader sends and is refused with (SimPool.sol): start the stage an exam earned, request the payout a stage
 *  earned. */
export const SIM_TRADER_ABI = [
  "function fund(address exam) returns (address stage)",
  "function claimPayout(address stage) returns (uint256 id)",
  "error NotTrader()",
  "error NotAnExam()",
  "error NotAStage()",
  "error Done()",
  "error NotPassed()",
  "error TooLate()",
  "error StillRunning()",
];

/** SimPool.Kind: what `accountOf(account).kind` says. */
export const KIND = Object.freeze({ none: 0, exam: 1, stage: 2 });

// ── looking through the chain for the wallet's accounts ───────────────────────────────────────────────────────

/** A node answers `eth_getLogs` over a window of this many blocks (app/lib/simpool.js, readLogs), and a page makes this
 *  many calls at a time: the stretch it looks through is `SCAN_BLOCKS`. */
export const SCAN_WINDOW = 50;
export const SCAN_CALLS = 20;
export const SCAN_BLOCKS = SCAN_WINDOW * SCAN_CALLS;

/**
 * The next stretch of blocks to look through: the newest `SCAN_BLOCKS` first, then the ones just before what has been
 * looked through. `loaded` is `{ from, top }` or null; `floor` is the pool's first block (0 when it is not known). Null
 * when nothing before `loaded.from` is left.
 */
export function scanPlan({ latest, floor = 0, loaded }) {
  if (!loaded) return { from: Math.max(floor, latest - SCAN_BLOCKS + 1), to: latest };
  if (loaded.from <= floor) return null;
  return { from: Math.max(floor, loaded.from - SCAN_BLOCKS), to: loaded.from - 1 };
}

/** Whether there is anything before what has been looked through: the control that looks further back is off when not. */
export const canLookBack = (loaded, floor = 0) => !loaded || loaded.from > floor;

/** What a page says of how far it looked. Only blocks that were read. */
export function scanNote(loaded, floor = 0) {
  if (!loaded) return "";
  if (loaded.from <= floor) return `Looked through every block of the pool, from block ${loaded.from} to block ${loaded.top}.`;
  return `Looked through blocks ${loaded.from} to ${loaded.top}. An exam bought earlier is not listed until you look further back.`;
}

/**
 * The accounts `wallet` may have, from the pool's events read (each `{ name, args, block, index }`) and the addresses this
 * browser remembers: exams the wallet bought, stages it started. Oldest first, an address once; a remembered address that
 * no event named has no block and goes last. The chain decides what each one is (`accountRows`): this only lists.
 */
export function candidatesOf(events, wallet, kept = []) {
  const mine = (a) => String(a ?? "").toLowerCase() === String(wallet).toLowerCase();
  const found = [
    ...soldExams(events).filter((x) => mine(x.trader)).map((x) => ({ address: x.exam, kind: "exam", block: x.block, key: x.block })),
    ...stages(events).filter((x) => mine(x.trader)).map((x) => ({ address: x.stage, kind: "stage", exam: x.exam, block: x.block, key: x.block })),
  ];
  const blockOf = new Map(found.map((c) => [c.address.toLowerCase(), c.block]));
  // The browser keeps its addresses in the order it saw them made (oldest first). One no event named has no block, but
  // it stands after the nearest kept address that has one (made after it), else before the nearest later one, else before
  // every account read: it is never taken for the newest only because its block is unknown.
  const order = [...new Set(kept.map((a) => String(a).toLowerCase()))];
  const extra = [];
  order.forEach((address, i) => {
    if (blockOf.has(address)) return;
    let key = null;
    for (let j = i - 1; j >= 0 && key === null; j--) if (blockOf.has(order[j])) key = blockOf.get(order[j]) + (i - j) / 1000;
    for (let j = i + 1; j < order.length && key === null; j++) if (blockOf.has(order[j])) key = blockOf.get(order[j]) - (j - i) / 1000;
    extra.push({ address, kind: null, block: null, key: key ?? -1 + i / 1000 });
  });
  return [...found, ...extra].sort((a, b) => a.key - b.key);
}

/** The addresses a browser keeps of the wallet's accounts in a pool, from what its storage holds (JSON text, or null):
 *  only well-formed addresses, whatever else the text says. The chain decides what each one is. */
export function remembered(raw) {
  try {
    const list = JSON.parse(raw ?? "[]");
    return Array.isArray(list) ? list.filter((a) => /^0x[0-9a-fA-F]{40}$/.test(a)) : [];
  } catch {
    return [];
  }
}

/** What storage holds after `address` is added: the newest `max` addresses, each once. */
export function remember(raw, address, max = 50) {
  const list = remembered(raw).filter((a) => a.toLowerCase() !== String(address).toLowerCase());
  return JSON.stringify([...list, address].slice(-max));
}

// ── what an account is ────────────────────────────────────────────────────────────────────────────────────

/** A length of time in the largest whole units, "3 days", "12 hours". */
export function span(seconds) {
  const s = Number(seconds);
  if (s % 86_400 === 0 && s >= 86_400) return `${s / 86_400} day${s === 86_400 ? "" : "s"}`;
  if (s % 3600 === 0 && s >= 3600) return `${s / 3600} hour${s === 3600 ? "" : "s"}`;
  return `${Math.max(1, Math.round(s / 60))} min`;
}

/**
 * Where an account stands, from its own `status` (PaperExam.Status: 1 active, 2 passed, 3 breached, 4 expired) and the
 * pool's `done`. `key` is for the markup; `label` is what is said; `note` is the sentence under it. A passed exam may
 * start its stage within `fundWindow` seconds of passing; a passed stage may be requested once.
 */
export function accountState(read, { now, fundWindow }) {
  const status = STATUS[Number(read.status)] ?? "none";
  const stage = Number(read.kind) === KIND.stage;
  if (read.unread) return { key: "unread", label: "Not read", note: "This account could not be read from the chain just now. Try again.", action: null };
  if (status === "active") return { key: "running", label: "Running", note: "", action: null };
  if (status === "breached") return { key: "failed", label: "Not passed", note: "A rule of the account was broken.", action: null };
  if (status === "expired") return { key: "expired", label: "Expired", note: "Its time ran out.", action: null };
  if (status !== "passed") return { key: "none", label: "Not started", note: "", action: null };
  if (stage) {
    return read.done
      ? { key: "passed", label: "Passed", note: "The payout was requested: see My payouts.", action: null }
      : { key: "passed", label: "Passed", note: "The payout can be requested now.", action: "claim" };
  }
  if (read.done) {
    return read.funded === false
      ? { key: "lapsed", label: "Passed", note: "The stage was not started in time.", action: null }
      : { key: "passed", label: "Passed", note: "The stage on the same size was started.", action: null };
  }
  const left = Number(read.passedAt) + Number(fundWindow);
  return Number(now) <= left
    ? { key: "passed", label: "Passed", note: `Start the stage on the same size within ${span(fundWindow)} of passing.`, action: "fund" }
    : { key: "lapsed", label: "Passed", note: "The stage was not started in time.", action: null };
}

/**
 * The wallet's accounts as rows, newest first. `candidates` is `candidatesOf`; `reads` maps each address in lower case
 * to what the chain says of it: `{ kind, line, done }` (the pool's `accountOf`), `{ status, trader, passedAt }` (the
 * account's `result()`); `lines` is the pool's menu by line: `[{ size, tier }]` (`lineInfo`). An address the chain does
 * not know as an account of the pool, or one that is not the wallet's, is not a row.
 */
export function accountRows({ candidates, reads, wallet, lines, now, fundWindow }) {
  const funded = new Set(candidates.filter((c) => c.kind === "stage" && c.exam).map((c) => c.exam.toLowerCase()));
  const rows = [];
  for (const c of candidates) {
    const r = reads.get(c.address.toLowerCase());
    if (!r || ![KIND.exam, KIND.stage].includes(Number(r.kind))) continue;
    if (String(r.trader).toLowerCase() !== String(wallet).toLowerCase()) continue;
    const line = lines[Number(r.line)];
    if (!line) continue;
    const stage = Number(r.kind) === KIND.stage;
    // An exam known to have started its stage says so; one whose Funded event was not read is not called unfunded.
    const state = accountState({ ...r, funded: funded.has(c.address.toLowerCase()) ? true : undefined }, { now, fundWindow });
    rows.push({
      address: c.address, kind: stage ? "stage" : "exam", step: stage ? "Stage" : "Exam", line: Number(r.line),
      size: line.size, tier: line.tier, state, block: c.block, key: c.key ?? c.block ?? -1, href: `#/exam/${c.address}`,
    });
  }
  return rows.sort((a, b) => b.key - a.key);
}

/** The pool's menu as the lists need it: each line's size and tier, in the pool's order (`menu()` and `tiers()`). */
export function lineInfo(menu, tiers) {
  if (menu.length !== tiers.length) throw new Error(`the pool's menu has ${menu.length} lines and its tiers ${tiers.length}`);
  return [...menu].map((m, i) => ({ size: BigInt(m.size ?? m[0]), tier: tierOf(tiers[i]) }));
}

/** The account Trade opens: the newest one still running, else the newest of all; null with none. */
export function tradeTarget(rows) {
  const running = rows.find((r) => r.state.key === "running");
  return (running ?? rows[0] ?? null)?.address ?? null;
}

// ── the payouts the pool holds for the wallet ─────────────────────────────────────────────────────────────────

/**
 * One payout in words. `p` is a payout of `payouts()` (app/lib/simpool.js); `reason` is the number the stop's own event
 * gave, or null where that was not read. The sum is the payout's own: the pool wrote it when the payout was requested.
 * A time is only ever "not before": the money moves when the contract's rules and the pool's money allow, never at an hour.
 */
export function payoutLine(p, now, reason = null) {
  const base = { id: p.id, account: p.account, amountText: dollars(p.amount) };
  // The contract's words end without a full stop: one is put where the sentence goes on.
  const said = reason == null ? "The reason is recorded on chain with the stop" : stopReasonText(reason);
  const why = /[.!?]$/.test(said) ? said : `${said}.`;
  switch (p.status) {
    case "waiting":
      return p.approved === true
        ? { ...base, key: "approved", label: "Approved", when: `Not before ${utc(p.dueAt)}`,
          note: "The approver approved it. It is paid from that time, once the pool's money covers it." }
        : { ...base, key: "waiting", label: "Waiting", when: `Not before ${utc(p.dueAt)}`,
          note: `A payout waits ${span(PAYOUT_DELAY)} from its request, then for the approver; one not approved goes out by itself ${span(PAYOUT_HARD)} after the request.` };
    case "approval":
      return { ...base, key: "waiting", label: "Waiting for approval", when: `Not before ${utc(p.hardAt)}`,
        note: `The ${span(PAYOUT_DELAY)} are over. It is paid once the approver approves it, or by itself ${span(PAYOUT_HARD)} after the request.` };
    case "queued":
      return { ...base, key: "approved", label: "Due", when: null,
        note: "It is due and in the queue. It is sent when the pool's money covers it, in the order of the requests, within the pool's daily limit." };
    case "paid":
      return { ...base, key: "paid", label: "Paid", when: null, note: "Sent to your wallet." };
    case "stopped":
      return { ...base, key: "stopped", label: "Stopped", when: p.reserveEnds ? `Final not before ${utc(p.reserveEnds)}` : null,
        note: `${why} For ${span(STOP_RESERVE)} after the stop you may object, and the checker may lift it.` };
    case "refused":
      return { ...base, key: "stopped", label: "Refused", when: null,
        note: `${why} The stop was not lifted within ${span(STOP_RESERVE)}: the refusal is final.` };
    case "held":
      return { ...base, key: "held", label: "Set aside", when: null,
        note: "The token refused the transfer. The money is set aside for you in the pool." };
    default:
      return { ...base, key: "none", label: "Unknown", when: null, note: "The pool answered a state this page has no words for." };
  }
}

/** The wallet's payouts, newest first, from the pool's `payout(id)` for each id (`structs`, in id order) and the reasons
 *  of the stops that were read (`reasons`: a Map from a payout's id to the number). */
export function payoutRows({ structs, wallet, now, reasons = new Map() }) {
  return payouts(structs, [], now, new Map())
    .filter((p) => String(p.trader).toLowerCase() === String(wallet).toLowerCase())
    .map((p) => payoutLine(p, now, reasons.get(p.id) ?? null))
    .reverse();
}

/** The stages of the wallet whose payout may be requested now: no sum on them, the pool writes it when it is requested. */
export const requestable = (rows) => rows.filter((r) => r.kind === "stage" && r.state.action === "claim");

// ── what a refused call says ──────────────────────────────────────────────────────────────────────────────────

/** The pool's refusal of `fund` or `claimPayout` in words; "" for one this page has no words for. */
export function traderRefusal(name) {
  switch (name) {
    case "NotTrader": return "Only the wallet that bought this exam can start its stage.";
    case "NotAnExam": return "This is not an exam of this pool.";
    case "NotAStage": return "This is not a stage of this pool.";
    case "Done": return "This was already done: the stage is started, or the payout requested.";
    case "NotPassed": return "It has not been passed.";
    case "TooLate": return `The time to start the stage is over.`;
    case "StillRunning": return "It is still running.";
    default: return "";
  }
}

// ── markup ────────────────────────────────────────────────────────────────────────────────────────────────

/** A line of the exams list for one account: what it is, and Trade, with the stage's start where the pool allows it. */
function examRowHtml(r) {
  const act = r.state.action === "fund" ? `<button type="button" class="sp-act" data-fund="${esc(r.address)}">Start the stage</button>`
    : r.state.action === "claim" ? `<button type="button" class="sp-act" data-claim="${esc(r.address)}">Request payout</button>` : "";
  return `<div class="sp-row" data-account="${esc(r.address)}" data-kind="${esc(r.kind)}" data-state="${esc(r.state.key)}">`
    + `<div class="sp-c sz" data-l="Size">${esc(dollars(r.size))}</div>`
    + `<div class="sp-c" data-l="Tier">${esc(r.tier.label)}</div>`
    + `<div class="sp-c" data-l="Step">${esc(r.step)}</div>`
    + `<div class="sp-c" data-l="State"><span class="sp-state ${esc(r.state.key)}">${esc(r.state.label)}</span>${r.state.note ? `<div class="sp-why">${esc(r.state.note)}</div>` : ""}</div>`
    + `<div class="sp-c go"><a class="sp-trade" href="${esc(r.href)}">Trade</a>${act}</div></div>`;
}

/** My exams: the wallet's exams and stages in the pool, one line each. */
export function examsHtml(rows) {
  const head = `<div class="sp-rowhead"><span>Size</span><span>Tier</span><span>Step</span><span>State</span><span></span></div>`;
  return `<div class="sp-box sp-list" id="exams"><div class="sp-bh"><b>My exams · ${rows.length}</b>`
    + `<span>exams and stages of this wallet in this pool · read from the chain</span></div>`
    + (rows.length ? head + rows.map(examRowHtml).join("") : `<p class="sp-empty">No exam of this wallet was found in the blocks looked through.</p>`)
    + `</div>`;
}

function payoutRowHtml(p) {
  return `<div class="sp-row" data-payout="${p.id}" data-state="${esc(p.key)}">`
    + `<div class="sp-c sz" data-l="Payout">#${p.id}</div>`
    + `<div class="sp-c" data-l="State"><span class="sp-state ${esc(p.key)}">${esc(p.label)}</span></div>`
    + `<div class="sp-c" data-l="Amount">${esc(p.amountText)}</div>`
    + `<div class="sp-c" data-l="When">${p.when ? esc(p.when) : "—"}</div>`
    + `<div class="sp-c wide"><div class="sp-why">${esc(p.note)}</div></div></div>`;
}

/** The stage that has passed and has no payout yet: the call that writes it, no sum. */
function readyRowHtml(r) {
  return `<div class="sp-row" data-ready="${esc(r.address)}" data-state="ready">`
    + `<div class="sp-c sz" data-l="Stage">${esc(dollars(r.size))}</div>`
    + `<div class="sp-c" data-l="State"><span class="sp-state ready">Passed</span></div>`
    + `<div class="sp-c" data-l="Amount">—</div><div class="sp-c" data-l="When">—</div>`
    + `<div class="sp-c go"><button type="button" class="sp-act" data-claim="${esc(r.address)}">Request payout</button></div>`
    + `<div class="sp-c wide"><div class="sp-why">Passed, not requested yet. The pool writes the payout when you request it.</div></div></div>`;
}

/** My payouts: the stages ready to be requested, then the payouts the pool holds for the wallet. */
export function payoutsHtml({ ready, rows }) {
  const head = `<div class="sp-rowhead pay"><span>Payout</span><span>State</span><span>Amount</span><span>When</span><span></span></div>`;
  const body = ready.map(readyRowHtml).join("") + rows.map(payoutRowHtml).join("");
  return `<div class="sp-box sp-list" id="payouts"><div class="sp-bh"><b>My payouts · ${ready.length + rows.length}</b>`
    + `<span>from the pool's contract · a time is "not before", never an hour</span></div>`
    + (body ? head + body : `<p class="sp-empty">No payout is held for this wallet, and no passed stage waits for its request.</p>`)
    + `</div>`;
}

/** A panel page's one message under the header: what is missing, and what to do. */
export function noteHtml(title, text) {
  return `<section class="sp-msg"><h2>${esc(title)}</h2><p>${esc(text)}</p></section>`;
}
