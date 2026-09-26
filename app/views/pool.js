// One pool: its terms and rules, the investor's controls, buying a challenge, and the funded
// stage with its stop.
import { CONFIG } from "../config.js";
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { sendUsdc } from "../lib/hlsend.js";
import { esc, render, $, wire, badge, row, settle } from "../lib/ui.js";
import { rulesAndTerms, termsHtml, rulesHtml } from "./pools.js";
import { tradePanel, stopInputs, equityPanel } from "./trading.js";
import { saleBlocker, topUpAdvice } from "../lib/funding.js";
import { stageName as nameOf, stageWords, isFundedStage, awaitingKeyWords } from "../lib/stages.js";

export async function poolView(address, page) {
  const pool = chain.contract("pool", address);
  if (!(await chain.factory().isPool(address))) {
    render(page, `<section class="card"><h2>Not a pool</h2><p>${esc(address)} was not created by this factory.</p></section>`);
    return;
  }
  const me = chain.currentAddress();
  const [{ rules, terms, assets }, stage, owner, ready, challenge, fundedTrader, earned, spotUsdc, fee,
         neededSpot, fundedEndReason] = await Promise.all([
    rulesAndTerms(pool), pool.stage(), pool.owner(), pool.accountReady(), pool.challenge(),
    pool.fundedTrader(), pool.earned(), hl.spotUsdc(address), chain.factory().challengeFee(), pool.capitalNeeded(),
    // What the contract wrote down if it stopped the funded trader; None when it ended clean.
    pool.fundedEndReason(),
  ]);
  const stageName = nameOf(stage);
  // Only a pool of the newer factory can be in stage 4, and only such a pool has the clock to read.
  const waiting = Number(stage) === 4
    ? await Promise.all([pool.passedAt(), pool.AWAIT_KEY_WINDOW()]).then(([passedAt, window]) => ({ passedAt, window }))
    : null;
  const doing = waiting ? awaitingKeyWords({ ...waiting, now: Math.floor(Date.now() / 1000) }) : stageWords(stage);
  const isOwner = chain.same(me, owner);
  const isFunded = chain.same(me, fundedTrader);
  // Challenge capital, funded capital, and 1 USDC for creating the challenge's account.
  const needed = Number(neededSpot) / 1e8;
  // The pool pays the challenge capital out of HyperCore, so an idle pool that is short there
  // would take the approval and then revert. Decided once, for the balance row, the buy button
  // and the card that replaces it.
  const blocker = saleBlocker({ stage, ready, challenge, spot: spotUsdc, needed });
  const shortOnCore = blocker && blocker.kind === "underfunded" ? blocker.short : 0;

  render(page, `
    <section class="card">
      <h2>Pool ${esc(chain.short(address))} ${badge(stageName, Number(stage) === 0 ? "ok" : "")}</h2>
      <p class="muted mono">${esc(address)}</p>
      ${row("Investor", `<span class="mono">${esc(owner)}</span>`)}
      ${row("HyperCore spot", `${esc(spotUsdc.toFixed(2))} USDC (needs ${esc(needed.toFixed(2))} to sell a challenge)${
        shortOnCore > 0 ? ` — <strong>${esc(shortOnCore.toFixed(2))} short</strong>. Creating each challenge's
        account costs 1 USDC here and never comes back, while the price the trader paid is held on HyperEVM.
        The investor moves it across; nothing on chain does.` : ""}`)}
      ${row("Account prepared", ready ? "yes" : "no")}
      ${Number(stage) === 1 ? row("Current challenge", `<a href="#/challenge/${esc(challenge)}">${esc(chain.short(challenge))}</a>`) : ""}
      ${isFundedStage(stage) ? row("Funded trader", `<span class="mono">${esc(fundedTrader)}</span>`) : ""}
      ${waiting ? row("Trader who passed", `<span class="mono">${esc(fundedTrader)}</span>`) : ""}
      ${row("What the pool is doing", esc(doing))}
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
  if (!blocker) {
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
      blocker.kind === "taken" ? `The pool is taken (${esc(stageName)}).`
        : blocker.kind === "not-prepared" ? "The investor hasn't prepared the account yet."
        : blocker.kind === "settling" ? "A previous challenge is still settling."
        : `The pool is ${esc(blocker.short.toFixed(2))} USDC short on HyperCore: it holds ${
            esc(spotUsdc.toFixed(2))} and needs ${esc(needed.toFixed(2))} to sell a challenge. Only its investor can top it up.`
    }</p>`;
  }

  // Funded stage
  const funded = $("#funded", page);
  if (Number(stage) === 2 || Number(stage) === 3) {
    funded.innerHTML = `<h3>Funded stage</h3><div id="equity"></div><div id="trade"></div><div class="actions" id="funded-actions"></div>`;
    // Closing is what a pool looks like after its funded stage was stopped: the panel should say
    // so rather than judge what is left of the account.
    settle(equityPanel($("#equity", page), pool, address,
                       { recorded: fundedEndReason, stopped: Number(stage) === 3 }),
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
  const advice = topUpAdvice({ short: shortOnCore, earned: Number(earned) / 1e6 });
  inv.innerHTML = `<h3>Investor</h3>
    <p>Capital goes to the pool on HyperCore: a spot transfer of USDC from your HyperCore account to
      <span class="mono">${esc(address)}</span>. The button below asks your wallet to sign that transfer;
      you can also make it yourself in the <a href="${esc(CONFIG.hlApp)}" target="_blank" rel="noopener">Hyperliquid testnet app</a>.
      Don't send USDC to this address on HyperEVM: the bridge doesn't credit contracts, and it would be lost.</p>
    <div class="inline"><input id="dep" type="number" step="0.01" min="0" placeholder="USDC"><button id="dep-btn">Send on HyperCore</button></div>
    <p><button id="prepare" class="secondary">Prepare the account</button>
      <span class="muted">Once the pool has USDC on HyperCore: separate spot and perp balances, approve the builder fee.</span></p>
    <p>Challenge income held in the contract: ${chain.usd6(earned)} USDC <button id="earned" class="secondary">Withdraw it</button></p>
    ${advice ? `<p class="muted">The pool is ${esc(advice.short.toFixed(2))} USDC short on HyperCore for the next
      challenge. This is normal after a cycle and is not a loss: creating the challenge's account costs 1 USDC on
      HyperCore, which never comes back, while the price the trader paid is held here on HyperEVM. The two sides
      don't meet on their own. ${advice.fromEarned > 0
        ? `Withdraw the ${esc(advice.fromEarned.toFixed(2))} USDC above, then send it to the pool with the field at the top of this card.`
        : "There is no income held here to cover it."}${advice.stillNeeded > 0
        ? ` That still leaves ${esc(advice.stillNeeded.toFixed(2))} USDC to come from you.` : ""}</p>` : ""}
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
