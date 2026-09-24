// One pool: its terms and rules, the investor's controls, buying a challenge, and the funded
// stage with its stop.
import { CONFIG } from "../config.js";
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { sendUsdc } from "../lib/hlsend.js";
import { esc, render, $, wire, badge, row, settle } from "../lib/ui.js";
import { rulesAndTerms, termsHtml, rulesHtml } from "./pools.js";
import { tradePanel, stopInputs, equityPanel } from "./trading.js";

export async function poolView(address, page) {
  const pool = chain.contract("pool", address);
  if (!(await chain.factory().isPool(address))) {
    render(page, `<section class="card"><h2>Not a pool</h2><p>${esc(address)} was not created by this factory.</p></section>`);
    return;
  }
  const me = chain.currentAddress();
  const [{ rules, terms, assets }, stage, owner, ready, challenge, fundedTrader, earned, spotUsdc, fee, neededSpot] = await Promise.all([
    rulesAndTerms(pool), pool.stage(), pool.owner(), pool.accountReady(), pool.challenge(),
    pool.fundedTrader(), pool.earned(), hl.spotUsdc(address), chain.factory().challengeFee(), pool.capitalNeeded(),
  ]);
  const stageName = chain.STAGE[Number(stage)];
  const isOwner = chain.same(me, owner);
  const isFunded = chain.same(me, fundedTrader);
  // Challenge capital, funded capital, and 1 USDC for creating the challenge's account.
  const needed = Number(neededSpot) / 1e8;

  render(page, `
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
  const buy = $("#buy", page);
  if (Number(stage) === 0 && ready && challenge === "0x0000000000000000000000000000000000000000") {
    buy.innerHTML = `<h3>Take the challenge</h3>
      <p>You pay ${chain.usd6(terms.price)} USDC on HyperEVM from your wallet${
        fee > 0n ? `, plus the platform's fee of ${chain.usd6(fee)} USDC, which isn't refunded` : ""}. The pool moves
      ${chain.usd6(terms.capital)} USDC to a new challenge account on HyperCore; a trading key is
      reserved for you. You never hold it: the pool gateway does, and your orders go through it,
      signed by your wallet. A profit share is paid to your address on HyperCore; if you have no
      account there yet, 1 USDC of it pays for creating one.</p>
      <button id="buy-btn">Pay and start</button>`;
    wire($("#buy-btn", page), async () => {
      // The pool pulls the price and the fee, so it is approved for exactly both.
      await chain.approveIfNeeded(address, terms.price + fee);
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
  const funded = $("#funded", page);
  if (Number(stage) === 2 || Number(stage) === 3) {
    funded.innerHTML = `<h3>Funded stage</h3><div id="equity"></div><div id="trade"></div><div class="actions" id="funded-actions"></div>`;
    // Closing is what a pool looks like after its funded stage was stopped: the panel should say
    // so rather than judge what is left of the account.
    settle(equityPanel($("#equity", page), pool, address, { stopped: Number(stage) === 3 }),
           $("#equity", page));
    if (Number(stage) === 2 && isFunded) settle(tradePanel($("#trade", page), address, rules), $("#trade", page));
    const actions = $("#funded-actions", page);
    if (Number(stage) === 2) {
      actions.innerHTML = `<button id="breach">Stop: a rule is broken</button>
        ${isOwner || isFunded ? '<button id="stop" class="secondary">End the funded stage</button>' : ""}
        <button id="checkpoint" class="secondary">Take today's snapshot</button>`;
      wire($("#breach", page), async () => {
        const { cancels, extra } = await stopInputs(address, rules);
        await chain.write("pool", address, "breach", [cancels, extra, chain.randomSalt()]);
        return "Stopped. Now settle until the pool is idle.";
      });
      wire($("#stop", page), async () => {
        const { cancels, extra } = await stopInputs(address, rules);
        await chain.write("pool", address, "stopFunded", [cancels, extra, chain.randomSalt()]);
        return "Ended. Now settle until the pool is idle.";
      }, { confirm: "End the funded stage? The trading key is retired for good." });
      wire($("#checkpoint", page), async () => {
        await chain.write("pool", address, "checkpoint");
        return "Snapshot taken.";
      });
    } else {
      actions.innerHTML = `<button id="settle">Settle one step</button>
        <button id="recut" class="secondary">Replace the agent again</button>`;
      wire($("#settle", page), async () => {
        const { cancels, extra } = await stopInputs(address, rules);
        await chain.write("pool", address, "settleFunded", [cancels, extra]);
        return "Step sent. HyperCore needs a few seconds; repeat until the pool is idle.";
      });
      wire($("#recut", page), async () => {
        await chain.write("pool", address, "recut", [chain.randomSalt()]);
        return "Agent replaced again.";
      });
    }
  } else {
    funded.remove();
  }

  // Investor
  const inv = $("#investor", page);
  if (!isOwner) {
    inv.remove();
    return;
  }
  inv.innerHTML = `<h3>Investor</h3>
    <p>Capital goes to the pool on HyperCore: a spot transfer of USDC from your HyperCore account to
      <span class="mono">${esc(address)}</span>. The button below asks your wallet to sign that transfer;
      you can also make it yourself in the <a href="${esc(CONFIG.hlApp)}" target="_blank" rel="noopener">Hyperliquid testnet app</a>.
      Don't send USDC to this address on HyperEVM: the bridge doesn't credit contracts, and it would be lost.</p>
    <div class="inline"><input id="dep" type="number" step="0.01" min="0" placeholder="USDC"><button id="dep-btn">Send on HyperCore</button></div>
    <p><button id="prepare" class="secondary">Prepare the account</button>
      <span class="muted">Once the pool has USDC on HyperCore: separate spot and perp balances, approve the builder fee.</span></p>
    <p>Challenge income held in the contract: ${chain.usd6(earned)} USDC <button id="earned" class="secondary">Withdraw it</button></p>
    <div class="inline"><input id="wd" type="number" step="0.01" min="0" placeholder="USDC"><button id="wd-btn" class="secondary">Withdraw on HyperCore</button></div>`;
  wire($("#dep-btn", page), async () => {
    const signer = chain.currentSigner() || (await chain.connect(), chain.currentSigner());
    await sendUsdc(signer, window.ethers.getAddress(address), hl.canonical($("#dep", page).value));
    return "Sent. The pool's HyperCore balance shows it in a few seconds; reload to see it.";
  });
  wire($("#prepare", page), async () => {
    await chain.write("pool", address, "prepareAccount");
    return "Prepared.";
  });
  wire($("#earned", page), async () => {
    await chain.write("pool", address, "withdrawEarned");
    return "Withdrawn.";
  });
  wire($("#wd-btn", page), async () => {
    const amount = chain.toUnits($("#wd", page).value, 8);
    await chain.write("pool", address, "withdrawOnCore", [amount]);
    return "Sent to your HyperCore account.";
  });
}
