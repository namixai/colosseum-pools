// The trader's other pages of a pool of exams, in the user panel's header (mockups 01 and 03): Trade (opens the screen of
// the exam the connected wallet trades), My exams (the wallet's exams and stages in the pool) and My payouts (what the pool
// holds for the wallet). Reading, and two calls the contract allows the trader: start the stage an exam earned, request
// the payout a stage earned. The states and the words are app/lib/simtrader.js; this file reads the pool and draws.
import * as chain from "../lib/chain.js";
import { CONFIG } from "../config.js";
import { esc, render, $, friendly, refusalName } from "../lib/ui.js";
import {
  SIM_POOL_ABI, SIM_POOL_APPROVAL_ABI, readLogs, firstBlockAt, MIN_TERM,
} from "../lib/simpool.js";
import { SIM_LINES_ABI } from "../lib/simlines.js";
import { userHeaderHtml, footerHtml } from "../lib/simpanel.js";
import { networkMarks } from "../lib/examscreen.js";
import {
  ACCOUNT_ABI, SIM_TRADER_ABI, HELD_ABI, KIND, SCAN_WINDOW, SCAN_CALLS, scanPlan, canLookBack, scanNote, candidatesOf, accountRows, lineInfo,
  tradeTarget, payoutRows, requestable, traderRefusal, claimTarget, remembered, remember, examsHtml, payoutsHtml, noteHtml,
} from "../lib/simtrader.js";
import { accountLabel, wireShell } from "./simshell.js";

const { ethers } = window;
// The pool's own getters and events, with the payout the pool of this repository answers (its eight fields), and the two
// calls of the trader. `payout` is declared once: the first, shorter form of SIM_POOL_ABI would not decode the answer.
const ABI = [...SIM_POOL_ABI.filter((l) => !l.startsWith("function payout(")), ...SIM_LINES_ABI, ...SIM_POOL_APPROVAL_ABI, ...SIM_TRADER_ABI, ...HELD_ABI];
const IFACE = new ethers.Interface(ABI);

const storeKey = (pool, wallet) => `sim-accounts-${String(pool).toLowerCase()}-${String(wallet).toLowerCase()}`;

function keptAddresses(pool, wallet) {
  try {
    return remembered(localStorage.getItem(storeKey(pool, wallet)));
  } catch {
    return [];
  }
}

/** Keeps an address of the wallet's in this browser, so a purchase older than the blocks looked through is still listed.
 *  The chain says what it is: a kept address is only where to ask. */
export function keepAddress(pool, wallet, address) {
  try {
    localStorage.setItem(storeKey(pool, wallet), remember(localStorage.getItem(storeKey(pool, wallet)), address));
  } catch {
    // storage is blocked: the list is then what the blocks looked through name
  }
}

/** The accounts a receipt names (an exam sold, a stage started), kept for the wallet. */
export function keepFromReceipt(pool, wallet, receipt) {
  for (const log of receipt?.logs ?? []) {
    if (String(log.address).toLowerCase() !== String(pool).toLowerCase()) continue;
    let parsed = null;
    try {
      parsed = IFACE.parseLog(log);
    } catch {
      parsed = null;
    }
    if (parsed?.name === "ExamSold") keepAddress(pool, wallet, parsed.args.exam);
    if (parsed?.name === "Funded") keepAddress(pool, wallet, parsed.args.stage);
  }
}

// ── reading ───────────────────────────────────────────────────────────────────────────────────────────────

const clock = () => chain.readProvider.getBlock("latest");

/** The pool's first block, from its term: the pool opened `MIN_TERM` before `lockedUntil`. 0 where that cannot be read. */
async function firstBlockOf(pool, latest) {
  try {
    const opened = Number(await pool.lockedUntil()) - MIN_TERM;
    const block = await firstBlockAt(opened, { low: 0, high: latest, timeOf: async (n) => (await chain.readProvider.getBlock(n))?.timestamp ?? 0 });
    return block ?? 0;
  } catch {
    return 0;
  }
}

/** What the trader's pages hold of the pool and the wallet: the contract, the menu, and what has been looked through. */
async function open(at, wallet) {
  const pool = new ethers.Contract(at, ABI, chain.readProvider);
  const [menu, tiers, fundWindow, head] = await Promise.all([pool.menu(), pool.tiers(), pool.fundWindow(), clock()]);
  return {
    at, wallet, pool, lines: lineInfo(menu, tiers), fundWindow: Number(fundWindow), floor: await firstBlockOf(pool, head.number),
    loaded: null, events: [], rows: [], now: head.timestamp, flight: false,
  };
}

/** Looks through the next stretch of blocks for the wallet's exams and stages, and asks the chain what each address is. */
async function lookBack(st) {
  const head = await clock();
  const plan = scanPlan({ latest: head.number, floor: st.floor, loaded: st.loaded });
  if (plan) {
    const { logs, complete } = await readLogs({ from: plan.from, to: plan.to, window: SCAN_WINDOW, maxCalls: SCAN_CALLS,
      getLogs: (x, y) => chain.readProvider.getLogs({ address: st.at, fromBlock: x, toBlock: y }) });
    if (!complete) throw new Error("the node did not answer for every block of the stretch");
    for (const l of logs) {
      let parsed = null;
      try {
        parsed = IFACE.parseLog(l);
      } catch {
        parsed = null;
      }
      if (parsed && (parsed.name === "ExamSold" || parsed.name === "Funded")) {
        st.events.push({ name: parsed.name, args: parsed.args, block: l.blockNumber, index: l.index });
      }
    }
    st.loaded = st.loaded ? { from: Math.min(st.loaded.from, plan.from), top: Math.max(st.loaded.top, plan.to) } : { from: plan.from, top: plan.to };
  }
  await refreshRows(st);
}

/** The rows again, from what the chain says of each address now. */
async function refreshRows(st) {
  const head = await clock();
  st.now = head.timestamp;
  const candidates = candidatesOf(st.events, st.wallet, keptAddresses(st.at, st.wallet));
  const reads = new Map();
  await chain.readAll(candidates, async (c) => {
    // The pool answers for any address: kind "none" is an address that is no account of this pool, which is no row. A
    // read that fails is not that answer, and is not taken for it: it is thrown, and the list is not cut short.
    const a = await st.pool.accountOf(c.address);
    if (Number(a.kind) === KIND.none) return;
    const known = { kind: Number(a.kind), line: Number(a.line), done: Boolean(a.done) };
    try {
      const r = await new ethers.Contract(c.address, ACCOUNT_ABI, chain.readProvider).result();
      reads.set(c.address.toLowerCase(), { ...known, status: Number(r.status), trader: r.trader, passedAt: Number(r.passedAt) });
    } catch {
      // a known account whose own state could not be read stays in the list, marked "Not read"
      reads.set(c.address.toLowerCase(), { ...known, unread: true, trader: st.wallet });
    }
  });
  st.rows = accountRows({ candidates, reads, wallet: st.wallet, lines: st.lines, now: st.now, fundWindow: st.fundWindow });
}

// ── the page around a section ────────────────────────────────────────────────────────────────────────────

/** Draws the user panel's header and footer round `body`. `current` is the menu's key; `path` follows the pool's route. */
function shell(page, at, current, path, body) {
  const marks = networkMarks(CONFIG);
  render(page, `<div class="sp sp-user">${userHeaderHtml({ address: at, account: accountLabel(), current, route: `#/sim/${at}${path}`, marks })}`
    + `${body}${footerHtml({ marks })}</div>`);
  wireShell(page);
}

const hero = (at, kick, title, lede) => `<section class="sp-hero"><div class="sp-kick">${esc(kick)} · Pool ${esc(chain.short(at))} · Hyperliquid testnet</div>`
  + `<h1>${title}</h1><p class="sp-lede">${lede}</p></section>`;

/** A page for a visitor with no wallet: the panel opens by wallet, and the header's button connects. */
function needWallet(page, at, current, path, what) {
  shell(page, at, current, path, noteHtml(what, "Connect your wallet to see what it holds in this pool."));
}

/** Where the money set aside goes, from the field: an address that is none, or whose capitals do not match its checksum, is
 *  told so and nothing is sent. The checksum is kept: a mistyped capital is how a mistyped address looks. */
function heldTarget(page) {
  const typed = claimTarget($("#sim-held-to", page)?.value);
  if (typed.error) return typed;
  try {
    return { to: ethers.getAddress(typed.to) };
  } catch {
    return { error: "The capital and small letters of this address do not match its checksum. Check it, or type it all in small letters." };
  }
}

/** The line under the call's outcome that says the lists could not be read again (and nothing when they could). */
function refreshNote(status, text) {
  let note = status.parentElement.querySelector("#sim-refresh");
  if (!note) {
    if (!text) return;
    note = document.createElement("p");
    note.id = "sim-refresh";
    note.className = "sp-status err";
    note.setAttribute("role", "status");
    status.insertAdjacentElement("afterend", note);
  }
  note.textContent = text;
}

/** The call a button sends: connects first and alone when there is no wallet (the page is drawn again when it does),
 *  then sends one call at a time, keeps what the receipt names, and reads again. */
function wireCalls(page, st, status, again) {
  page.addEventListener("click", async (e) => {
    const button = e.target.closest("button[data-fund], button[data-claim], button[data-held]");
    if (!button || button.disabled || st.flight) return;
    if (!chain.currentSigner()) {
      try {
        await chain.connect();
      } catch (err) {
        status.className = "sp-status err";
        status.textContent = friendly(err);
      }
      return;
    }
    const fund = button.dataset.fund !== undefined;
    const held = button.dataset.held !== undefined;
    // Where the money set aside goes is read before anything is sent: an address that is none is told so, and nothing is asked of the wallet.
    const target = held ? heldTarget(page) : null;
    if (target?.error) {
      status.className = "sp-status err";
      status.textContent = target.error;
      return;
    }
    st.flight = true;
    page.querySelectorAll("button[data-fund], button[data-claim], button[data-held]").forEach((b) => { b.disabled = true; });
    status.className = "sp-status busy";
    status.textContent = "Working…";
    try {
      const signer = chain.currentSigner();
      const pool = new ethers.Contract(st.at, ABI, signer);
      const tx = held ? await pool.claimHeld(target.to) : fund ? await pool.fund(button.dataset.fund) : await pool.claimPayout(button.dataset.claim);
      const receipt = await tx.wait();
      chain.forgetLiveReads();
      if (!receipt || receipt.status !== 1) throw new Error("the call reverted");
      keepFromReceipt(st.at, st.wallet, receipt);
      status.className = "sp-status ok";
      // The money is gone from the pool: its box goes now, whether or not the lists can be read again.
      if (held) $("#held", page)?.remove();
      status.textContent = held ? `The money set aside was sent to ${target.to} (transaction ${receipt.hash}).`
        : fund ? `The stage is started (transaction ${receipt.hash}).` : `The payout is requested (transaction ${receipt.hash}).`;
    } catch (err) {
      // A call that was refused may have been asked about a sum that was read a moment ago: the lists read what is there now.
      chain.forgetLiveReads();
      status.className = "sp-status err";
      status.textContent = traderRefusal(refusalName(err, IFACE)) || friendly(err);
    }
    // The flight lasts until the lists are read again, so a press on a row that is about to change is not taken; the
    // buttons come back whether or not the reading worked. What the call came to stays on the screen; a reading that
    // fails is said apart from it.
    try {
      await again();
      refreshNote(status, "");
    } catch (err) {
      refreshNote(status, `Could not refresh: ${friendly(err)}`);
    } finally {
      st.flight = false;
      page.querySelectorAll("button[data-fund], button[data-claim], button[data-held]").forEach((b) => { b.disabled = false; });
    }
  });
}

/** The button that looks through the stretch of blocks before what has been looked through. */
function wireBack(page, st, status, draw) {
  $("#sim-back", page).addEventListener("click", async () => {
    if (st.flight) return;
    st.flight = true;
    $("#sim-back", page).disabled = true;
    status.className = "sp-status busy";
    status.textContent = "Looking through earlier blocks…";
    try {
      await lookBack(st);
      status.textContent = "";
    } catch (err) {
      status.className = "sp-status err";
      status.textContent = `Could not look: ${friendly(err)}`;
    } finally {
      st.flight = false;
      // A page that cannot be drawn again says so, and the button comes back all the same.
      try {
        await draw();
      } catch (err) {
        status.className = "sp-status err";
        status.textContent = `Could not refresh: ${friendly(err)}`;
      } finally {
        $("#sim-back", page).disabled = !canLookBack(st.loaded, st.floor);
      }
    }
  });
}

// ── Trade ─────────────────────────────────────────────────────────────────────────────────────────────────

/** #/sim/<pool>/trade: the wallet's trading screen. It opens the exam the wallet trades; a wallet with none is told so. */
export async function simTradeView(address, page) {
  const at = ethers.getAddress(address);
  if (!chain.currentAddress()) return needWallet(page, at, "trade", "/trade", "Trade");
  const st = await open(at, chain.currentAddress());
  await lookBack(st);
  const target = tradeTarget(st.rows);
  if (target) {
    // Replaced, not pushed: Back from the trading screen must not come to this page and be sent on again.
    location.replace(`${location.href.split("#")[0]}#/exam/${target}`);
    return;
  }
  shell(page, at, "trade", "/trade", noteHtml("Trade",
    `No exam of this wallet was found in the blocks looked through (${scanNote(st.loaded, st.floor) || "none"}). `
    + "Buy one on the Pool page, or look further back in My exams."));
}

// ── My exams ──────────────────────────────────────────────────────────────────────────────────────────────

/** #/sim/<pool>/exams: the wallet's exams and stages in this pool: size, tier, step, state, and Trade. */
export async function simExamsView(address, page) {
  const at = ethers.getAddress(address);
  if (!chain.currentAddress()) return needWallet(page, at, "exams", "/exams", "My exams");
  const st = await open(at, chain.currentAddress());
  await lookBack(st);
  shell(page, at, "exams", "/exams", `${hero(at, "My exams", "Your<br><em>exams</em>",
    "Each line is an exam, or the stage that follows it on the same size. <b>Trade</b> opens its trading screen.")}
    <div id="sim-list"></div>
    <p class="sp-scan"><span id="sim-scan"></span> <button type="button" class="sp-act" id="sim-back">Look further back</button></p>
    <p id="sim-status" class="sp-status" role="status"></p>`);
  const status = $("#sim-status", page);
  const draw = async () => {
    $("#sim-list", page).innerHTML = examsHtml(st.rows);
    $("#sim-scan", page).textContent = scanNote(st.loaded, st.floor);
    $("#sim-back", page).disabled = !canLookBack(st.loaded, st.floor);
  };
  await draw();
  wireBack(page, st, status, draw);
  wireCalls(page, st, status, async () => { await refreshRows(st); draw(); });
}

// ── My payouts ────────────────────────────────────────────────────────────────────────────────────────────

/** The reason of each stop of the wallet's payouts, from the stop's own event: the block of a stop is the one at its
 *  time (`stoppedAt`), found by halving, and the event is read in the few blocks from there. A stop whose event was not
 *  read has no reason shown: the page says it is recorded on chain, and does not make one up. */
async function stopReasons(st, list, latest) {
  const reasons = new Map();
  const topic = IFACE.getEvent("PayoutStopped").topicHash;
  await Promise.all(list.filter((p) => p.stoppedAt).map(async (p) => {
    try {
      const block = await firstBlockAt(p.stoppedAt, { low: Math.max(0, st.floor), high: latest,
        timeOf: async (n) => (await chain.readProvider.getBlock(n))?.timestamp ?? 0 });
      if (block === null) return;
      const logs = await chain.readProvider.getLogs({ address: st.at, fromBlock: block, toBlock: Math.min(latest, block + SCAN_WINDOW - 1),
        topics: [topic, ethers.zeroPadValue(ethers.toBeHex(p.id), 32)] });
      const parsed = logs.length ? IFACE.parseLog(logs[0]) : null;
      if (parsed) reasons.set(p.id, Number(parsed.args.reason));
    } catch {
      // not read: the payout says where the reason is
    }
  }));
  return reasons;
}

/** #/sim/<pool>/payouts: the pool's payouts for the wallet, by state, and the passed stages that may be requested. */
export async function simPayoutsView(address, page) {
  const at = ethers.getAddress(address);
  if (!chain.currentAddress()) return needWallet(page, at, "payouts", "/payouts", "My payouts");
  const st = await open(at, chain.currentAddress());
  await lookBack(st);
  shell(page, at, "payouts", "/payouts", `${hero(at, "My payouts", "Your<br><em>payouts</em>",
    "A payout is requested once a stage is passed. After that it waits for its time and the approver, then the pool sends it.")}
    <div id="sim-list"></div>
    <p class="sp-scan"><span id="sim-scan"></span> <button type="button" class="sp-act" id="sim-back">Look further back</button></p>
    <p id="sim-status" class="sp-status" role="status"></p>`);
  const status = $("#sim-status", page);
  const draw = async () => {
    const head = await clock();
    const count = Number(await st.pool.payoutCount());
    const structs = await chain.readAll(Array.from({ length: count }, (_, i) => i), (id) => st.pool.payout(id));
    // The payouts of the wallet, whose stops' reasons are read where there are stops.
    const now = head.timestamp;
    // What the pool set aside for the wallet, read by the wallet's address: a wallet with nothing set aside sees no Claim.
    // A balance that cannot be read costs the Claim box, not the lists: the page says it was not read.
    let held = null;
    try {
      held = BigInt(await st.pool.heldOf(st.wallet));
    } catch {
      held = null;
    }
    const mine = payoutRows({ structs, wallet: st.wallet, now });
    const stoppedIds = new Set(mine.filter((p) => p.key === "stopped").map((p) => p.id));
    const reasons = stoppedIds.size
      ? await stopReasons(st, structs.map((s, id) => ({ id, stoppedAt: Number(s.stoppedAt ?? 0), trader: s.trader })).filter((p) => stoppedIds.has(p.id)), head.number)
      : new Map();
    await refreshRows(st);
    const field = $("#sim-held-to", page);
    const typed = field?.value;
    const focus = field && document.activeElement === field ? { from: field.selectionStart, to: field.selectionEnd, dir: field.selectionDirection } : null;
    $("#sim-list", page).innerHTML = payoutsHtml({ ready: requestable(st.rows), rows: payoutRows({ structs, wallet: st.wallet, now, reasons, heldLeft: held === null ? null : held > 0n }),
      held, wallet: st.wallet });
    // The address the wallet was typing stays where it was when the lists are drawn again.
    const again = $("#sim-held-to", page);
    if (typed !== undefined && again) {
      again.value = typed;
      if (focus) {
        again.focus();
        try {
          again.setSelectionRange(focus.from, focus.to, focus.dir ?? undefined);
        } catch {
          // the field takes no selection: the focus is enough
        }
      }
    }
    $("#sim-scan", page).textContent = scanNote(st.loaded, st.floor);
    $("#sim-back", page).disabled = !canLookBack(st.loaded, st.floor);
  };
  await draw();
  wireBack(page, st, status, draw);
  wireCalls(page, st, status, draw);
}
