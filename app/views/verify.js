// "Check it yourself": what anyone can read without trusting us, and exactly where our word
// still carries weight.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { esc, render, $, badge, row, isAddress, settle, wire } from "../lib/ui.js";
import { ruleVerdict, liveReadingIsMoot, pastFundedStage } from "../lib/verdict.js";
import { stageWords } from "../lib/stages.js";
import { keyFacts } from "../lib/keys.js";

export async function verifyView(address, page) {
  if (!isAddress(address)) {
    render(page, `<section class="card narrow"><h2>Check it yourself</h2>
      <p>Open this page from a pool or a challenge, or paste an account address:</p>
      <div class="inline"><input id="addr" placeholder="0x…"><button id="go">Check</button></div></section>`);
    $("#go", page).addEventListener("click", () => { location.hash = `#/verify/${$("#addr", page).value.trim()}`; });
    page.insertAdjacentHTML("beforeend", `<section class="card"><h3>Who holds the keys</h3>${KEYS_HELD}</section>`);
    return;
  }
  const [isPool, isChallenge] = chain.deployed()
    ? await Promise.all([chain.factory().isPool(address), chain.factory().isChallenge(address)])
    : [false, false];
  const kind = isPool ? "pool" : isChallenge ? "challenge" : null;
  render(page, `
    <section class="card">
      <h2>Check it yourself</h2>
      <p class="mono">${esc(address)} ${badge(kind || "not ours", kind ? "ok" : "bad")}</p>
      <p class="muted">Everything below is read in your browser from HyperEVM testnet and Hyperliquid's public
      API. Where a step still rests on our word, it says so.</p>
    </section>
    <section class="card"><h3>1. Who can trade this account</h3><div id="keys" class="muted">Reading…</div></section>
    <section class="card"><h3>2. Trades against the rules</h3><div id="fills" class="muted">Reading…</div></section>
    <section class="card"><h3>3. Who holds the keys</h3>${KEYS_HELD}</section>`);
  if (!kind) {
    $("#keys", page).textContent = chain.deployed()
      ? "This address was not created by the factory; there is nothing to check."
      : "The contracts are not deployed yet.";
    $("#fills", page).textContent = "";
    return;
  }
  const account = chain.contract(kind, address);
  // Raw errors used to be pasted in here, ethers payload and all. settle() says what failed
  // in words; the section that failed is named by the box it writes into.
  settle(keysPanel(account, address, page), $("#keys", page));
  settle(fillsPanel(account, address, page, kind), $("#fills", page));
}

const KEYS_HELD = `
  <p>In this demo the keys that trade these accounts are testnet keys held by our pool gateway. Before it
  signs, the gateway's own code checks the platform's caps: the asset list, a size cap per asset and
  400 USDC per order. Nothing on this page can show you that; it rests on our word.</p>
  <p class="small muted">Usenami Signer, our enclave signing service, is a separate product and takes no part
  in this demo. Its code is public in <code>namixai/signer</code>.</p>`;

async function keysPanel(account, address, page) {
  const reg = chain.registry();
  // Every read this panel makes on load, in one place: four from the chain, plus Hyperliquid's
  // own answer for each key it found. No event log — see app/lib/keys.js for why.
  const facts = await keyFacts({
    agentKey: () => account.agentKey(),
    keyOf: () => reg.keyOf(address),
    cutKey: () => account.cutKey(),
    cutBlock: () => account.cutBlock(),
    bindingOf: (key) => reg.bindingOf(key),
    role: (key) => hl.info({ type: "userRole", user: key }),
  });

  const rows = facts.keys.map((k) => {
    const hlSays = !k.role
      ? badge("Hyperliquid: no answer right now", "bad")
      : k.role.role === "agent"
      ? (chain.same(k.role.data.user, address)
        ? badge("agent of this account", "ok")
        : badge(`agent of ${chain.short(k.role.data.user)}`, "bad"))
      : badge(`Hyperliquid: ${k.role.role}`);
    const tags = [
      k.isCurrent ? badge("approved now", "ok") : "",
      k.wasCut ? badge("cut by a stop", "bad") : "",
    ].join("");
    return `<div class="kv"><span class="mono">${esc(k.address)}</span><span>
      ${tags}
      ${badge(chain.KEY_STATE[Number(k.binding.state)], Number(k.binding.state) === 2 ? "ok" : "")}
      trader <span class="mono">${esc(chain.short(k.binding.trader))}</span> · ${hlSays}</span></div>`;
  });

  const stop = facts.cutBlock
    ? row("Last stop, as the account recorded it",
      `block ${facts.cutBlock}, agent replaced by an address nobody holds`
      + (facts.cutKey ? `; the key it cut was <span class="mono">${esc(facts.cutKey)}</span>` : ""))
    : row("Stops recorded by this account", "none");

  $("#keys", page).className = "";
  $("#keys", page).innerHTML = `
    ${row("Agent key the contract has approved now",
      facts.current ? `<span class="mono">${esc(facts.current)}</span>` : "none")}
    ${stop}
    <h4>Keys this account can name without the event log</h4>${rows.join("") || "<p>none</p>"}
    <p class="small muted">"Hyperliquid:" is Hyperliquid's own answer to <code>userRole</code> for each key, so you
    can see a replacement took effect without asking us. What you can't see from here: who holds each key.
    In this demo our gateway does.</p>
    <p class="small muted">Everything above is contract state, read now, in ${facts.chainReads} reads from the
    chain: four for the account and the registry, and one binding for each key named. The account writes down
    the block of its last stop and the key it cut, so that much is exact and does not decay. The full list of
    keys ever bound lives in the event log, and this public RPC serves logs 50 blocks at a call — over the
    hundreds of thousands of blocks since the deployment that is thousands of calls, which no browser gets to
    make. We don't pretend to read it here.</p>
    ${facts.cutBlock ? '<p><button type="button" id="log">Read the stop from the chain</button></p>' : ""}
    <div id="logout"></div>`;

  if (facts.cutBlock) wire($("#log", page), () => keyLog(account, facts.cutBlock, page), { done: "" });
}

/**
 * The stop, on an explicit click. One `eth_getLogs` over the single block the account wrote
 * down, so it is always found and always affordable. There is no windowed scan anywhere on
 * this page: one that fits a browser covers a fraction of a percent of the history and shrinks
 * every day, and the answer it fails to find is one the account states outright.
 */
async function keyLog(account, cutBlock, page) {
  const cuts = await account.queryFilter(account.filters.AgentCut(), cutBlock, cutBlock);
  const rows = cuts.map((ev) => `<div class="kv"><span>block ${ev.blockNumber}</span>
    <span>replaced ${esc(chain.short(ev.args.oldKey))} with keyless
    <span class="mono">${esc(chain.short(ev.args.keyless))}</span></span></div>`);
  $("#logout", page).innerHTML = `
    <h4>AgentCut in block ${cutBlock}</h4>${rows.join("")
      || "<p>The account records this block, but the node served no event for it.</p>"}`;
  return "";
}

async function fillsPanel(account, address, page, kind) {
  const [rules, fills, list] = await Promise.all([account.rules(), hl.fills(address), hl.perps()]);
  const allowed = new Set(rules.assets.map(Number));
  const rows = fills.slice(0, 200).map((f) => {
    const p = list.find((x) => x.name === f.coin);
    const ok = p && allowed.has(p.index);
    const notional = Number(f.sz) * Number(f.px);
    return `<tr><td>${esc(new Date(f.time).toISOString().slice(0, 19).replace("T", " "))}</td>
      <td>${esc(f.coin)}</td><td>${esc(f.side === "B" ? "buy" : "sell")}</td><td>${esc(f.sz)}</td><td>${esc(f.px)}</td>
      <td>${esc(notional.toFixed(2))}</td><td>${esc(f.closedPnl)}</td><td>${ok ? badge("allowed", "ok") : badge("not allowed", "bad")}</td></tr>`;
  });
  const bad = fills.filter((f) => { const p = list.find((x) => x.name === f.coin); return !(p && allowed.has(p.index)); }).length;
  // A settled account holds nothing, and a verdict computed on nothing says "drawdown" whatever
  // the stop was for. What the contract wrote down when it stopped the account is the answer;
  // the live reading is for accounts that are still trading.
  const [live, recorded, stopped, state, cutBlock] = await Promise.all([
    account.violation([]),
    kind === "challenge" ? account.breachReason() : account.fundedEndReason(),
    account.isStopped(),
    kind === "challenge" ? account.status() : account.stage(),
    account.cutBlock(),
  ]);
  // A pool records its own reason when it stops a funded trader (fundedEndReason), so a stopped
  // pool has one to show; a funded stage ended without a rule broken leaves it None and the pool
  // is simply stopped. "Finished" is for challenges: past Active one never trades again, while a
  // pool goes back to Idle and sells the next challenge.
  // A pool that has funded someone keeps the block where it cut their key, and keeps it after the
  // stage is over. Without that, a pool whose funded stage ended CLEANLY records reason None and
  // reads exactly like a pool that has never funded anyone -- which is what a judge arriving after
  // a completed cycle saw here. Non-zero cutBlock on an idle pool means: a funded stage ran, and
  // it is over.
  const idleAfterFunding = pastFundedStage({ kind, stage: state, cutBlock });
  const finished = kind === "challenge" ? Number(state) > 2 : idleAfterFunding;
  // Every line below is about the LAST funded stage once the pool is idle again, never about the
  // pool as it stands: idle means it is ready to sell the next challenge.
  const past = idleAfterFunding ? "the last funded stage" : "this account";
  const verdict = ruleVerdict({ recorded, live, stopped, finished });
  $("#fills", page).className = "";
  $("#fills", page).innerHTML = `
    ${kind === "pool" ? row("What the pool is doing", esc(stageWords(state))) : ""}
    ${row("Fills on this account (Hyperliquid API)", String(fills.length))}
    ${row("Fills in an asset outside the rules", bad ? badge(String(bad), "bad") : badge("0", "ok"))}
    ${verdict.kind === "recorded"
      ? row(`The rule the contract recorded when it stopped ${esc(past)}`,
            badge(chain.BREACH[verdict.reason], "bad"))
      : verdict.kind === "finished-with-no-rule-broken"
        ? row(`Rules while ${esc(past)} traded`, badge("none broken; the contract recorded no stop", "ok"))
        : verdict.kind === "stopped-without-a-recorded-reason"
          ? row("This account is stopped", badge("stopped; it keeps no reason of its own", "bad"))
          : row("The contract's verdict right now",
                verdict.reason ? badge(chain.BREACH[verdict.reason], "bad") : badge("inside the rules", "ok"))}
    ${idleAfterFunding ? row("Funded stage on this pool",
        `ended at block ${esc(String(cutBlock))}; the pool is idle again and can sell the next challenge`) : ""}
    ${liveReadingIsMoot(verdict) ? `<p class="small muted">A verdict this page could compute from the
      account's state would be about the account as it stands now${idleAfterFunding
        ? `, and this pool is idle: its capital is home and the stage above is over. A reading of what
        it holds today says nothing about how that stage went`
        : `. This one has stopped trading, and its
      settlement may still be running, so a reading of what is left on it does not say what happened while it
      traded`}. What the contract itself holds is above.</p>` : ""}
    <div class="scroll"><table><thead><tr><th>time (UTC)</th><th>asset</th><th>side</th><th>size</th><th>price</th>
    <th>notional</th><th>closed PnL</th><th>asset rule</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>
    <p class="small muted">This checks trades that reached Hyperliquid. An order our gateway refused never reaches
    the exchange, and neither does one that was signed and not sent, so this page can't say the gateway never
    signed something against the rules. It says what traded.</p>`;
}
