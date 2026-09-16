// One challenge: where it stands against the rules, the trader's ticket, and the buttons
// anyone can press (start, stop, pass, settle).
import * as chain from "../lib/chain.js";
import { esc, render, $, wire, badge, row, when, pct } from "../lib/ui.js";
import { rulesHtml, assetNames } from "./pools.js";
import { tradePanel, stopInputs, equityPanel } from "./trading.js";

export async function challengeView(address) {
  if (!(await chain.factory().isChallenge(address))) {
    render(`<section class="card"><h2>Not a challenge</h2><p>${esc(address)} was not created by this factory.</p></section>`);
    return;
  }
  const ch = chain.contract("challenge", address);
  const me = chain.currentAddress();
  const [status, reason, trader, pool, rules, terms, createdAt, deadline, key, payoutOwed, payoutSent, arrived] =
    await Promise.all([
      ch.status(), ch.breachReason(), ch.trader(), ch.pool(), ch.rules(), ch.terms(), ch.createdAt(),
      ch.deadline(), ch.agentKey(), ch.payoutOwed(), ch.payoutSent(), ch.capitalArrived(),
    ]);
  const s = Number(status);
  const name = chain.STATUS[s];
  const isTrader = chain.same(me, trader);
  const target = Number(terms.capital) / 1e6 * (1 + Number(terms.targetBps) / 1e4);
  const stopped = [3, 4, 5, 6, 7].includes(s);
  const tone = s === 2 ? "ok" : s === 6 || s === 8 ? "ok" : stopped ? "bad" : "";

  render(`
    <section class="card">
      <h2>Challenge ${esc(chain.short(address))} ${badge(name, tone)}</h2>
      <p class="muted mono">${esc(address)}</p>
      ${row("Trader", `<span class="mono">${esc(trader)}</span>${isTrader ? " (you)" : ""}`)}
      ${row("Pool", `<a href="#/pool/${esc(pool)}">${esc(chain.short(pool))}</a>`)}
      ${row("Capital", `${chain.usd6(terms.capital)} USDC, target ${esc(target.toFixed(2))} USDC (+${pct(terms.targetBps)})`)}
      ${row("Created", esc(when(createdAt)))}
      ${s >= 2 ? row("Deadline", esc(when(deadline))) : ""}
      ${row("Agent key", key === "0x0000000000000000000000000000000000000000" ? "none (cut)" : `<span class="mono">${esc(key)}</span>`)}
      ${s === 3 ? row("Stopped for", esc(chain.BREACH[Number(reason)])) : ""}
      ${s === 6 || s === 8 ? row("Trader's share", `${chain.usd8(payoutOwed)} USDC owed, ${chain.usd8(payoutSent)} sent`) : ""}
      <p><a href="#/verify/${esc(address)}">Check this account yourself →</a></p>
    </section>
    <section class="card"><h3>Rules</h3><div id="rules"></div></section>
    <section class="card" id="live"><h3>Now</h3><div id="equity" class="muted">Loading…</div></section>
    <section class="card" id="trade"></section>
    <section class="card"><h3>Actions</h3><div class="actions" id="actions"></div></section>`);

  $("#rules").innerHTML = rulesHtml(rules, await assetNames(rules.assets));

  if (s >= 2) equityPanel($("#equity"), ch, address);
  else $("#equity").textContent = arrived ? "The capital has arrived. Start the challenge." : "Waiting for the capital to reach HyperCore.";

  if (s === 2 && isTrader) tradePanel($("#trade"), address, rules);
  else $("#trade").remove();

  const actions = $("#actions");
  const buttons = [];
  if (s === 1) {
    buttons.push(`<button id="activate">Start</button>`);
    buttons.push(`<button id="abort" class="secondary">Abort (capital never arrived)</button>`);
  }
  if (s === 2) {
    buttons.push(`<button id="graduate">Pass</button>`);
    buttons.push(`<button id="breach">Stop: a rule is broken</button>`);
    buttons.push(`<button id="checkpoint" class="secondary">Take today's snapshot</button>`);
    buttons.push(`<button id="expire" class="secondary">End: time is up</button>`);
    if (isTrader) buttons.push(`<button id="forfeit" class="secondary">Give up</button>`);
  }
  if (stopped) {
    buttons.push(`<button id="settle">Settle one step</button>`);
    buttons.push(`<button id="recut" class="secondary">Replace the agent again</button>`);
  }
  actions.innerHTML = buttons.join("") || `<p class="muted">Nothing left to do.</p>`;

  wire($("#activate"), async () => {
    await chain.write("challenge", address, "activate");
    return "Started. Reload in a few seconds.";
  });
  wire($("#abort"), async () => {
    await chain.write("challenge", address, "abort");
    return "Aborted and refunded.";
  });
  wire($("#graduate"), async () => {
    await chain.write("challenge", address, "graduate", [chain.randomSalt()]);
    return "Passed. The pool funds the trader with a new key; settle this challenge next.";
  });
  wire($("#breach"), async () => {
    const { cancels, extra } = await stopInputs(address, rules);
    await chain.write("challenge", address, "breach", [cancels, extra, chain.randomSalt()]);
    return "Stopped. The next order signed with the old key will be refused by Hyperliquid.";
  });
  wire($("#checkpoint"), async () => {
    await chain.write("challenge", address, "checkpoint");
    return "Snapshot taken.";
  });
  wire($("#expire"), async () => {
    const { cancels, extra } = await stopInputs(address, rules);
    await chain.write("challenge", address, "expire", [cancels, extra, chain.randomSalt()]);
    return "Ended.";
  });
  wire($("#forfeit"), async () => {
    const { cancels, extra } = await stopInputs(address, rules);
    await chain.write("challenge", address, "forfeit", [cancels, extra, chain.randomSalt()]);
    return "Ended.";
  }, { confirm: "Give up this challenge? Your key is retired and the capital goes back to the pool." });
  wire($("#settle"), async () => {
    const { cancels, extra } = await stopInputs(address, rules);
    await chain.write("challenge", address, "settle", [cancels, extra]);
    return "Step sent. HyperCore needs a few seconds; repeat until the status is Settled.";
  });
  wire($("#recut"), async () => {
    await chain.write("challenge", address, "recut", [chain.randomSalt()]);
    return "Agent replaced again.";
  });
}

