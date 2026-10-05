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
import { stageName as nameOf, isFundedStage, awaitingKeyWords, poolStatus } from "../lib/stages.js";
import { outcomesHtml } from "../lib/outcomes.js";
import { poolKind, holdsCode, KIND_NOTE } from "../lib/listing.js";
import { agentTradability, agentNote } from "../lib/agentcap.js";
import { isArchive, ARCHIVE } from "../lib/deployments.js";
import {
  usdcTotal1e8, allOf, withdrawPlan, withdrawOutcome, arrivedBy, WITHDRAW_WAIT_MS, WITHDRAW_POLL_MS,
} from "../lib/withdraw.js";
import { usd } from "../lib/shared.js";

export async function poolView(address, page) {
  const pool = chain.contract("pool", address);
  // The live deployment or the archive: whichever factory made it. Everything below reads that one.
  const deployment = await chain.deploymentOf(address, "pool");
  if (!deployment) {
    render(page, `<section class="card"><h2>Not a pool</h2><p>${esc(address)} was not created by either factory this site
      reads.</p></section>`);
    return;
  }
  const archived = isArchive(deployment);
  const me = chain.currentAddress();
  const [{ rules, terms, assets }, stage, owner, ready, challenge, fundedTrader, earned, spotUsdc, fee,
         neededSpot, fundedEndReason] = await Promise.all([
    rulesAndTerms(pool), pool.stage(), pool.owner(), pool.accountReady(), pool.challenge(),
    pool.fundedTrader(), pool.earned(), hl.spotUsdc(address), chain.factory(undefined, deployment).challengeFee(), pool.capitalNeeded(),
    // What the contract wrote down if it stopped the funded trader; None when it ended clean.
    pool.fundedEndReason(),
  ]);
  const stageName = nameOf(stage);
  // Only a pool of the newer factory can be in stage 4, and only such a pool has the clock to read.
  const waiting = Number(stage) === 4
    ? await Promise.all([pool.passedAt(), pool.AWAIT_KEY_WINDOW()]).then(([passedAt, window]) => ({ passedAt, window }))
    : null;
  const isOwner = chain.same(me, owner);
  const isFunded = chain.same(me, fundedTrader);
  // Challenge capital, funded capital, and 1 USDC for creating the challenge's account.
  const needed = Number(neededSpot) / 1e8;
  // The pool pays the challenge capital out of HyperCore, so an idle pool that is short there
  // would take the approval and then revert. Decided once, for the balance row, the buy button
  // and the card that replaces it.
  // An archived pool sells nothing, whatever it holds: the deployment is the reason, and the buy button never appears.
  const blocker = archived ? { kind: "archived" } : saleBlocker({ stage, ready, challenge, spot: spotUsdc, needed });
  const shortOnCore = blocker && blocker.kind === "underfunded" ? blocker.short : 0;
  // The badge and the sentence come from the same reading as the buy button: an idle pool that cannot sell a
  // challenge says so, instead of "Idle: it can sell a challenge" under a row that names the gap.
  const status = poolStatus(stage, archived ? null : blocker, archived);
  const kind = poolKind({ owner, ownerIsContract: await holdsCode((a) => chain.readProvider.getCode(a), owner),
    deployer: deployment.deployer, address, liveRunPool: deployment.liveRunPool });
  const doing = waiting ? awaitingKeyWords({ ...waiting, now: Math.floor(Date.now() / 1000) }) : status.words;
  // Said under the pool's name and again in the buy card, before the price is paid: an agent that buys this challenge through the repository's
  // client could not send one order on it. An archived pool sells nothing, so it says nothing of the kind.
  const agent = archived ? "" : agentNote(agentTradability({ capital: Number(terms.capital) / 1e6,
    fundedCapital: Number(terms.fundedCapital) / 1e6, leverageX100: rules.maxLeverageX100 }));

  render(page, `
    <section class="card">
      <h2>Pool ${esc(chain.short(address))} ${badge(status.name, status.tone)}</h2>
      ${archived ? `<p class="notice">${esc(ARCHIVE.poolNote)}</p>` : ""}
      <p class="muted mono">${esc(address)}</p>
      ${KIND_NOTE[kind] ? `<p class="small"><strong>${esc(KIND_NOTE[kind])}</strong></p>` : ""}
      ${agent ? `<p class="notice">${esc(agent)}</p>` : ""}
      ${row("Investor", `<span class="mono">${esc(owner)}</span>`)}
      ${row("HyperCore spot", archived ? `${esc(spotUsdc.toFixed(2))} USDC`
        : `${esc(spotUsdc.toFixed(2))} USDC (needs ${esc(needed.toFixed(2))} to sell a challenge)${
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
      <section class="card"><h3>Terms</h3>${termsHtml(terms, archived ? null : fee)}</section>
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
      ${agent ? `<p class="notice">${esc(agent)}</p>` : ""}
      ${outcomesHtml(terms, fee)}
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
      blocker.kind === "archived" ? "No challenges are sold here: this pool belongs to the archived deployment."
        : blocker.kind === "taken" ? `The pool is taken (${esc(stageName)}).`
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
    // The gateway serves the live deployment only, so an archived pool's funded trader has no panel to trade from.
    if (Number(stage) === 2 && isFunded && !archived) settle(tradePanel($("#trade", page), address, rules), $("#trade", page));
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
  const poolSpot = await readPoolSpot(address);
  inv.innerHTML = `<h3>Investor</h3>
    ${archived ? "" : `    <p>Capital goes to the pool on HyperCore: a spot transfer of USDC from your HyperCore account to
      <span class="mono">${esc(address)}</span>. The button below asks your wallet to sign that transfer;
      you can also make it yourself in the <a href="${esc(CONFIG.hlApp)}" target="_blank" rel="noopener">Hyperliquid testnet app</a>.
      Don't send USDC to this address on HyperEVM: the bridge doesn't credit contracts, and it would be lost.</p>`}
    ${archived ? "" : `<div class="inline"><input id="dep" type="number" step="0.01" min="0" placeholder="USDC"><button id="dep-btn">Send on HyperCore</button></div>
    <p><button id="prepare" class="secondary">Prepare the account</button>
      <span class="muted">Once the pool has USDC on HyperCore: separate spot and perp balances, approve the builder fee.</span></p>`}
    <p>Challenge income held in the contract: ${chain.usd6(earned)} USDC <button id="earned" class="secondary">Withdraw it</button></p>
    ${advice ? `<p class="muted">The pool is ${esc(advice.short.toFixed(2))} USDC short on HyperCore for the next
      challenge. This is normal after a cycle and is not a loss: creating the challenge's account costs 1 USDC on
      HyperCore, which never comes back, while the price the trader paid is held here on HyperEVM. The two sides
      don't meet on their own. ${advice.fromEarned > 0
        ? `Withdraw the ${esc(advice.fromEarned.toFixed(2))} USDC above, then send it to the pool with the field at the top of this card.`
        : "There is no income held here to cover it."}${advice.stillNeeded > 0
        ? ` That still leaves ${esc(advice.stillNeeded.toFixed(2))} USDC to come from you.` : ""}</p>` : ""}
    <p class="small muted">The pool holds <span id="wd-balance">${esc(usd(poolSpot, 8))}</span> USDC on HyperCore. HyperCore
      drops a transfer above the balance without a word, so the page refuses one before your wallet is asked.</p>
    <div class="inline"><input id="wd" type="number" step="any" min="0" placeholder="USDC"><button id="wd-all" class="secondary" type="button">All</button><button id="wd-btn" class="secondary">Withdraw on HyperCore</button></div>`;
  // An archived pool keeps only the two ways out: what it earned, and what it holds on HyperCore.
  if (!archived) wireFunding(page, address);
  wire($("#earned", page), async () => {
    await chain.write("pool", address, "withdrawEarned");
    return "Withdrawn.";
  });
  $("#wd-all", page).addEventListener("click", async () => {
    const now = await readPoolSpot(address);
    $("#wd-balance", page).textContent = usd(now, 8);
    $("#wd", page).value = allOf(now);
  });
  wire($("#wd-btn", page), async () => {
    // Read again at the click: the balance on the page may be minutes old.
    const before = await readPoolSpot(address);
    $("#wd-balance", page).textContent = usd(before, 8);
    const amount = withdrawPlan($("#wd", page).value, before);
    await chain.write("pool", address, "withdrawOnCore", [amount]);
    // The transaction only asks. What HyperCore did is in the pool's balance, a few seconds later. From here on
    // the transaction is in a block: a reading that fails must not come out as a bare error, as if nothing had
    // been sent, so it is kept as a failed reading and the outcome is decided from all of them (lib/withdraw.js).
    const reads = [];
    for (let waited = 0; waited < WITHDRAW_WAIT_MS; waited += WITHDRAW_POLL_MS) {
      await new Promise((resolve) => setTimeout(resolve, WITHDRAW_POLL_MS));
      const now = await readPoolSpot(address).catch(() => null);
      reads.push(now);
      // Not on any fall: a challenge bought meanwhile takes its capital from the same balance, and stopping there
      // would report a withdrawal still on its way as one that fell short.
      if (arrivedBy(before, now, amount)) break;
    }
    const outcome = withdrawOutcome({ before, amount, reads });
    if (outcome.after !== undefined) $("#wd-balance", page).textContent = usd(outcome.after, 8);
    if (!outcome.ok) throw new Error(outcome.text);
    return outcome.text;
  });
}

/** The pool's spot USDC on HyperCore, exactly, in 1e8 units. */
async function readPoolSpot(address) {
  return usdcTotal1e8(await hl.spot(address));
}

/** The investor's two ways in -- capital on HyperCore and preparing the account -- for a pool that sells challenges. */
function wireFunding(page, address) {
  wire($("#dep-btn", page), async () => {
    const signer = chain.currentSigner() || (await chain.connect(), chain.currentSigner());
    await sendUsdc(signer, window.ethers.getAddress(address), hl.canonical($("#dep", page).value));
    return "Sent. The pool's HyperCore balance shows it in a few seconds; reload to see it.";
  });
  wire($("#prepare", page), async () => {
    await chain.write("pool", address, "prepareAccount");
    return "Prepared.";
  });
}
