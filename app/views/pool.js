// One pool: its terms and rules, the investor's controls, buying a challenge, and the funded
// stage with its stop.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { esc, render, $, wire, badge, row } from "../lib/ui.js";
import { rulesAndTerms, termsHtml, rulesHtml } from "./pools.js";
import { tradePanel, stopInputs, equityPanel } from "./trading.js";

export async function poolView(address) {
  const pool = chain.contract("pool", address);
  if (!(await chain.factory().isPool(address))) {
    render(`<section class="card"><h2>Not a pool</h2><p>${esc(address)} was not created by this factory.</p></section>`);
    return;
  }
  const me = chain.currentAddress();
  const [{ rules, terms, assets }, stage, owner, ready, challenge, fundedTrader, earned, spotUsdc] = await Promise.all([
    rulesAndTerms(pool), pool.stage(), pool.owner(), pool.accountReady(), pool.challenge(),
    pool.fundedTrader(), pool.earned(), hl.spotUsdc(address),
  ]);
  const stageName = chain.STAGE[Number(stage)];
  const isOwner = chain.same(me, owner);
  const isFunded = chain.same(me, fundedTrader);
  const needed = Number(terms.capital + terms.fundedCapital) / 1e6;

  render(`
    <section class="card">
      <h2>Pool ${esc(chain.short(address))} ${badge(stageName, Number(stage) === 0 ? "ok" : "")}</h2>
      <p class="muted mono">${esc(address)}</p>
      ${row("Investor", `<span class="mono">${esc(owner)}</span>`)}
      ${row("HyperCore spot", `${esc(spotUsdc.toFixed(2))} USDC (needs ${esc(needed.toFixed(2))} to sell a challenge)`)}
      ${row("Account prepared", ready ? "yes" : "no")}
      ${Number(stage) === 1 ? row("Current challenge", `<a href="#/challenge/${esc(challenge)}">${esc(chain.short(challenge))}</a>`) : ""}
      ${Number(stage) >= 2 ? row("Funded trader", `<span class="mono">${esc(fundedTrader)}</span>`) : ""}
      <p><a href="#/verify/${esc(address)}">Check this account yourself →</a></p>
    </section>
    <div class="grid">
      <section class="card"><h3>Terms</h3>${termsHtml(terms)}</section>
      <section class="card"><h3>Rules</h3>${rulesHtml(rules, assets)}</section>
    </div>
    <section class="card" id="buy"></section>
    <section class="card" id="funded"></section>
    <section class="card" id="investor"></section>`);

  // Trader: buy
  const buy = $("#buy");
  if (Number(stage) === 0 && ready && challenge === "0x0000000000000000000000000000000000000000") {
    buy.innerHTML = `<h3>Take the challenge</h3>
      <p>You pay ${chain.usd6(terms.price)} USDC on HyperEVM from your wallet. The pool moves
      ${chain.usd6(terms.capital)} USDC to a new challenge account on HyperCore; a trading key from the
      enclave is reserved for you. You never hold that key: your orders go through the pool gateway,
      signed by your wallet.</p>
      <label class="check"><input type="checkbox" id="us"> I am not a US person and I am not acting for one.</label>
      <button id="buy-btn">Pay and start</button>`;
    wire($("#buy-btn"), async () => {
      if (!$("#us").checked) throw new Error("Please confirm you are not a US person.");
      await chain.approveIfNeeded(address, terms.price);
      const receipt = await chain.write("pool", address, "buyChallenge");
      const log = receipt.logs
        .map((l) => { try { return pool.interface.parseLog(l); } catch { return null; } })
        .find((l) => l && l.name === "ChallengeSold");
      if (log) location.hash = `#/challenge/${log.args.challenge}`;
      return "Challenge bought.";
    });
  } else {
    buy.innerHTML = `<h3>Take the challenge</h3><p class="muted">${
      Number(stage) !== 0 ? `The pool is taken (${esc(stageName)}).` : !ready ? "The investor hasn't prepared the account yet." : "A previous challenge is still settling."
    }</p>`;
  }

  // Funded stage
  const funded = $("#funded");
  if (Number(stage) === 2 || Number(stage) === 3) {
    funded.innerHTML = `<h3>Funded stage</h3><div id="equity"></div><div id="trade"></div><div class="actions" id="funded-actions"></div>`;
    equityPanel($("#equity"), pool, address);
    if (Number(stage) === 2 && isFunded) tradePanel($("#trade"), address, rules);
    const actions = $("#funded-actions");
    if (Number(stage) === 2) {
      actions.innerHTML = `<button id="breach">Stop: a rule is broken</button>
        ${isOwner || isFunded ? '<button id="stop" class="secondary">End the funded stage</button>' : ""}
        <button id="checkpoint" class="secondary">Take today's snapshot</button>`;
      wire($("#breach"), async () => {
        const { cancels, extra } = await stopInputs(address, rules);
        await chain.write("pool", address, "breach", [cancels, extra, chain.randomSalt()]);
        return "Stopped. Now settle until the pool is idle.";
      });
      wire($("#stop"), async () => {
        const { cancels, extra } = await stopInputs(address, rules);
        await chain.write("pool", address, "stopFunded", [cancels, extra, chain.randomSalt()]);
        return "Ended. Now settle until the pool is idle.";
      }, { confirm: "End the funded stage? The trading key is retired for good." });
      wire($("#checkpoint"), async () => {
        await chain.write("pool", address, "checkpoint");
        return "Snapshot taken.";
      });
    } else {
      actions.innerHTML = `<button id="settle">Settle one step</button>
        <button id="recut" class="secondary">Replace the agent again</button>`;
      wire($("#settle"), async () => {
        const { cancels, extra } = await stopInputs(address, rules);
        await chain.write("pool", address, "settleFunded", [cancels, extra]);
        return "Step sent. HyperCore needs a few seconds; repeat until the pool is idle.";
      });
      wire($("#recut"), async () => {
        await chain.write("pool", address, "recut", [chain.randomSalt()]);
        return "Agent replaced again.";
      });
    }
  } else {
    funded.remove();
  }

  // Investor
  const inv = $("#investor");
  if (!isOwner) {
    inv.remove();
    return;
  }
  inv.innerHTML = `<h3>Investor</h3>
    <p>Add capital from HyperEVM USDC, or send USDC to <span class="mono">${esc(address)}</span> on HyperCore.</p>
    <div class="inline"><input id="dep" type="number" step="0.01" min="0" placeholder="USDC"><button id="dep-btn">Deposit</button></div>
    <p><button id="prepare" class="secondary">Prepare the account</button>
      <span class="muted">Once the pool has USDC on HyperCore: separate spot and perp balances, approve the builder fee.</span></p>
    <p>Challenge income held in the contract: ${chain.usd6(earned)} USDC <button id="earned" class="secondary">Withdraw it</button></p>
    <div class="inline"><input id="wd" type="number" step="0.01" min="0" placeholder="USDC"><button id="wd-btn" class="secondary">Withdraw on HyperCore</button></div>`;
  wire($("#dep-btn"), async () => {
    const amount = chain.toUnits($("#dep").value, 6);
    await chain.approveIfNeeded(address, amount);
    await chain.write("pool", address, "deposit", [amount]);
    return "Deposited. It shows on HyperCore after the next block.";
  });
  wire($("#prepare"), async () => {
    await chain.write("pool", address, "prepareAccount");
    return "Prepared.";
  });
  wire($("#earned"), async () => {
    await chain.write("pool", address, "withdrawEarned");
    return "Withdrawn.";
  });
  wire($("#wd-btn"), async () => {
    const amount = chain.toUnits($("#wd").value, 8);
    await chain.write("pool", address, "withdrawOnCore", [amount]);
    return "Sent to your HyperCore account.";
  });
}
