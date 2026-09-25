// The shared pool: many investors hold shares in one book of seats. What the pool holds, its seats,
// settlement points, and a holder's deposits, requests and payments. Testnet only, and not part of
// the core (docs/SHARED-POOL.md).
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { sendUsdc } from "../lib/hlsend.js";
import { esc, render, $, wire, badge, row, when, duration, pct, settle } from "../lib/ui.js";
import { rulesAndTerms, termsHtml, rulesHtml } from "./pools.js";
import {
  SHARED_POOL, SHARED_ABI, TICKET_STATE, blockerText, amount, spot1e8, shares, worth, price, depositPlan,
  ticketsToName, lockedUntil, paymentsLine, explain, plain, usd, openedTicket,
} from "../lib/shared.js";

const { ethers } = window;
const ZERO = "0x0000000000000000000000000000000000000000";
/** A holder's latest tickets, the ones the page lists. */
const TICKETS_SHOWN = 5;
/** Open tickets a settlement point started from this page looks at. Anyone can open tickets for the
 *  price of gas, so the list can be long; the viewer's own come first. */
const OPEN_READ = 40;
const DOC = "https://github.com/namixai/colosseum-pools/blob/main/docs/SHARED-POOL.md";

function contract(address, runner) {
  return new ethers.Contract(address, SHARED_ABI, runner || chain.readProvider);
}

/** An account's spot USDC on HyperCore, exactly, in 1e8 units. */
async function spotOf(address) {
  const state = await hl.spot(address);
  const usdc = (state.balances || []).find((b) => b.coin === "USDC");
  return usdc ? spot1e8(usdc.total) : 0n;
}

/** A write to the pool through the wallet. A revert of the pool's own comes back in words. */
async function write(address, method, args = []) {
  if (!chain.currentSigner()) await chain.connect();
  try {
    const tx = await contract(address, chain.currentSigner())[method](...args);
    const receipt = await tx.wait();
    if (!receipt || receipt.status !== 1) throw new Error(`${method} reverted`);
    return receipt;
  } catch (err) {
    const words = explain(err);
    throw words ? new Error(words) : err;
  }
}

export async function sharedView(address, page) {
  const at = ethers.getAddress(address || SHARED_POOL);
  const sp = contract(at);
  const [value, total, seed, queued, minDeposit, lock, feeBps, blocker, seatList, points, lastPoint, evm, core] =
    await Promise.all([
      sp.value(), sp.totalShares(), sp.seedShares(), sp.queuedShares(), sp.minDeposit(), sp.lock(), sp.feeBps(),
      sp.blocker(), sp.seats(), sp.points(), sp.lastPoint(), chain.usdc().balanceOf(at), spotOf(at),
    ]);
  const reason = Number(blocker[0]);

  render(page, `
    <section class="card">
      <h2>Shared pool ${badge("testnet, not reviewed")}</h2>
      <p class="muted mono">${esc(at)}</p>
      <p>Here many investors share one pool. Their money sits in seats: each seat is an ordinary pool
      of this site, with its own challenge and funded stage, and this contract owns all of them. A seat's
      rules and the price of its challenge go on the chain before the seat gets any money, and they never
      change after that. The operator can add seats. You don't set these limits. You look at the
      published ones and decide whether to come in.</p>
      <p class="muted">This part is new. It runs on Hyperliquid testnet with mock USDC, and nobody has
      reviewed it yet. <a href="${esc(DOC)}" target="_blank" rel="noopener">How it works</a>.</p>
    </section>
    <div class="grid">
      <section class="card">
        <h3>The pool</h3>
        ${row("Value", `${usd(value, 8)} USDC`)}
        ${row("Shares", shares(total))}
        ${row("Price of a share", `${price(value, total)} USDC`)}
        ${row("Free on HyperEVM", `${usd(evm, 6)} USDC`)}
        ${row("Free on HyperCore", `${usd(core, 8)} USDC`)}
        ${row("Waiting to be paid", `${shares(queued)} shares, worth ${usd(worth(queued, value, total), 8)} USDC`)}
        ${row("The platform's starting shares", `${shares(seed)}, worth ${usd(worth(seed, value, total), 8)} USDC; they never leave`)}
        ${row("Smallest deposit", `${usd(minDeposit, 8)} USDC`)}
        ${row("Lock", `${esc(duration(lock))} after your latest deposit`)}
        ${row("The platform's fee", `${pct(feeBps)} of your own profit, taken when you withdraw`)}
        <p class="small muted">The value is what the pool would have if it closed every account at the
        mark price now and paid every trader it owes.</p>
      </section>
      <section class="card">
        <h3>Settlement points</h3>
        <p>Deposits turn into shares, and withdrawals get paid, only at a settlement point. Anyone can
        run one when nothing is holding it up.</p>
        ${row("Points so far", esc(points))}
        ${row("The latest", esc(when(lastPoint)))}
        ${row("Can one run now?", `${badge(reason === 0 ? "yes" : "not now", reason === 0 ? "ok" : "bad")}
          ${esc(blockerText(reason))}${reason ? ` <span class="mono">${esc(chain.short(blocker[1]))}</span>` : ""}`)}
        <p><button id="point"${reason ? " disabled" : ""}>Run a settlement point</button></p>
      </section>
    </div>
    <section class="card" id="you"><h3>You</h3><p class="muted">Loading…</p></section>
    <section><h3>Seats</h3><div id="seats" class="grid"><p class="muted">Loading…</p></div></section>`);

  wire($("#point", page), async () => {
    const names = await ticketsWithDeposits(sp, minDeposit);
    await write(at, "settle", [names]);
    return names.length
      ? `Done: ${names.length} deposit(s) taken in. Payments on HyperCore land in a few seconds.`
      : "Done. Payments on HyperCore land in a few seconds.";
  });

  // One after the other: ethers sends reads made in the same moment as one JSON-RPC batch, and the
  // public RPC refuses a batch over twenty (app/lib/batch.js). Side by side, these two would pass it.
  await settle(holderPanel($("#you", page), sp, at, { value, total, minDeposit, lock }, chain.currentAddress()),
    $("#you", page));
  await settle(seats($("#seats", page), sp, seatList), $("#seats", page));
}

/** The open tickets holding a deposit a point can take in, the viewer's own first. */
async function ticketsWithDeposits(sp, minDeposit) {
  const count = Number(await sp.openTicketCount());
  const mine = [];
  const me = chain.currentAddress();
  if (me) {
    const n = Number(await sp.ticketCount(me));
    const recent = Array.from({ length: Math.min(n, TICKETS_SHOWN) }, (_, i) => n - 1 - i);
    mine.push(...(await chain.readAll(recent, (i) => sp.ticketAddress(me, i))));
  }
  const others = count ? [...(await sp.openTickets(0, Math.min(count, OPEN_READ)))] : [];
  const candidates = [...new Set([...mine, ...others])];
  const states = await chain.readAll(candidates, (t) => sp.tickets(t));
  const open = candidates.filter((_, i) => Number(states[i].state) === 1);
  const spots = await chain.readAll(open, (t) => spotOf(t), 5);
  return ticketsToName(open.map((t, i) => ({ address: t, spot: spots[i] })), minDeposit);
}

/** What `me` holds in the pool, has been paid and has on its way, with the forms to deposit and to
 *  ask to withdraw. The forms act through the connected wallet. */
export async function holderPanel(box, sp, at, pool, me) {
  if (!me) {
    box.innerHTML = `<h3>You</h3><p class="muted">Connect your wallet to deposit, ask to withdraw, or see
      what the pool has paid you.</p>`;
    return;
  }
  const [held, waiting, cost, last, count, paid] = await Promise.all([
    sp.sharesOf(me), sp.queuedOf(me), sp.basis(me), sp.lastDeposit(me), sp.ticketCount(me),
    // The pool deployed for the testnet run is older than this record: it reverts, and only that
    // means "no record". Anything else, a rate limit say, fails the panel with its own words.
    sp.payments(me).catch((err) => {
      if (err?.code === "CALL_EXCEPTION") return null;
      throw err;
    }),
  ]);
  const n = Number(count);
  const indices = Array.from({ length: Math.min(n, TICKETS_SHOWN) }, (_, i) => n - 1 - i);
  const tickets = await chain.readAll(indices, (i) => sp.ticketAddress(me, i));
  const states = await chain.readAll(tickets, (t) => sp.tickets(t));
  const spots = await chain.readAll(tickets, (t) => spotOf(t), 5);
  const free = BigInt(held) > BigInt(waiting) ? BigInt(held) - BigInt(waiting) : 0n;
  const until = lockedUntil(last, pool.lock);
  const now = Math.floor(Date.now() / 1000);

  let paidText;
  if (paid === null) {
    paidText = "This pool was deployed before the contract kept a record of payments. Your wallet and your HyperCore spot balance show what arrived.";
  } else if (Number(paid.at) === 0) {
    paidText = "Nothing yet.";
  } else {
    paidText = `${esc(paymentsLine(paid))}. The latest payment: ${esc(when(paid.at))}.`;
  }

  const ticketRows = tickets.map((t, i) => {
    const state = TICKET_STATE[Number(states[i].state)] || "?";
    const waitingToSend = state === "Open" && spots[i] === 0n;
    return `<div class="kv"><span class="mono">${esc(chain.short(t))}</span><span>${esc(state)}, ${usd(spots[i], 8)} USDC
      on HyperCore${waitingToSend ? ` <button class="small secondary" data-ticket="${esc(t)}">Send to it</button>` : ""}</span></div>`;
  }).join("");

  box.innerHTML = `<h3>You</h3>
    ${row("Your shares", `${shares(held)}, worth ${usd(worth(held, pool.value, pool.total), 8)} USDC now`)}
    ${row("What they cost you", `${usd(cost, 8)} USDC`)}
    ${row("Waiting to be paid", `${shares(waiting)} shares`)}
    ${row("Paid to you so far", paidText)}
    <h4>Deposit</h4>
    <p>Each deposit goes to an address of its own, a ticket. Your wallet opens the ticket on HyperEVM,
    then signs a USDC transfer to it on HyperCore. HyperCore charges 1 USDC for creating the ticket's
    account, so a deposit of ${usd(pool.minDeposit, 8)} costs you
    ${usd(BigInt(pool.minDeposit) + 100_000_000n, 8)}. At the next settlement point the deposit turns into
    shares, priced at what the pool was worth without it.</p>
    <div class="inline"><input id="dep" type="number" step="0.01" min="${esc(plain(pool.minDeposit, 8))}"
      placeholder="USDC"><button id="dep-btn">Open a ticket and send</button></div>
    ${ticketRows ? `<p class="small muted">Your latest tickets:</p>${ticketRows}` : ""}
    <h4>Withdraw</h4>
    <p>Ask for some of your shares or all of them. Until they're paid they still gain and lose with the
    pool. Each settlement point pays everyone waiting at that point's price. If the pool's free money
    doesn't cover everyone, each person gets the same fraction and the rest waits for the next point.
    The pool pays from its HyperEVM USDC first and the rest on HyperCore, so one payment can land in two
    places. You can't take a request back.</p>
    ${Number(last) && until > now ? `<p class="muted">You can ask after ${esc(when(until))}.</p>` : ""}
    <div class="inline"><input id="wd" type="number" step="any" min="0" value="${esc(plain(free, 8))}"
      placeholder="shares"><button id="wd-btn" class="secondary">Ask to withdraw</button></div>`;

  wire($("#dep-btn", box), async () => {
    const text = hl.canonical($("#dep", box).value);
    depositPlan(text, pool.minDeposit);
    const signer = chain.currentSigner() || (await chain.connect(), chain.currentSigner());
    const receipt = await write(at, "openTicket");
    const parsed = receipt.logs.map((l) => { try { return sp.interface.parseLog(l); } catch { return null; } });
    const ticket = ethers.getAddress(openedTicket(parsed, signer.address));
    await sendUsdc(signer, ticket, text);
    return `Sent to your ticket ${chain.short(ticket)}. It becomes shares at the next settlement point.`;
  });
  box.querySelectorAll("button[data-ticket]").forEach((btn) => {
    wire(btn, async () => {
      const text = hl.canonical($("#dep", box).value);
      depositPlan(text, pool.minDeposit);
      const signer = chain.currentSigner() || (await chain.connect(), chain.currentSigner());
      await sendUsdc(signer, ethers.getAddress(btn.dataset.ticket), text);
      return "Sent. It becomes shares at the next settlement point.";
    });
  });
  wire($("#wd-btn", box), async () => {
    const asked = chain.toUnits($("#wd", box).value, 8);
    await write(at, "requestRedeem", [asked]);
    return "Asked. The next settlement point pays as much as the pool has free.";
  });
}

async function seats(box, sp, seatList) {
  if (!seatList.length) {
    box.innerHTML = `<p class="muted">No seats yet.</p>`;
    return;
  }
  const cards = await chain.readAll([...seatList], async (seat) => {
    const pool = chain.contract("pool", seat);
    const [{ rules, terms, assets }, stage, challenge, term, spot] = await Promise.all([
      rulesAndTerms(pool), pool.stage(), pool.challenge(), sp.fundedTerm(seat), spotOf(seat),
    ]);
    let status = "";
    if (challenge !== ZERO) {
      const ch = chain.contract("challenge", challenge);
      const [s, deadline] = await Promise.all([ch.status(), ch.deadline()]);
      status = row("Challenge", `<span class="mono">${esc(chain.short(challenge))}</span>, ${esc(chain.STATUS[Number(s)])}${
        Number(deadline) ? `, until ${esc(when(deadline))}` : ""}`);
    }
    return `<article class="card">
      <h3><span class="mono">${esc(chain.short(seat))}</span> ${badge(chain.STAGE[Number(stage)], Number(stage) === 0 ? "ok" : "")}</h3>
      ${row("On HyperCore spot", `${usd(spot, 8)} USDC`)}
      ${status}
      ${row("Longest funded stage", esc(duration(term)))}
      ${termsHtml(terms)}
      ${rulesHtml(rules, assets)}
      <p class="small muted">This seat was made by the shared pool's own factory. The challenge and
      trading pages of this site only know the demo's factory, so they won't open it.</p>
    </article>`;
  }, 3); // five reads a seat at once, three seats a round: fifteen in one batch
  box.innerHTML = cards.join("");
}

