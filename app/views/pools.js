// The list of pools, and the form that creates one.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { esc, render, $, wire, pct, duration, badge, row } from "../lib/ui.js";

function notDeployed(page) {
  render(page, `<section class="card"><h2>Not deployed yet</h2>
    <p>The contracts are not on testnet yet, so there is nothing to list. The addresses go into
    <code>app/config.js</code> after deployment.</p></section>`);
}

export async function assetNames(indices) {
  const names = await Promise.all(indices.map((i) => hl.perp(Number(i)).then((p) => p.name)));
  return names.join(", ");
}

export async function rulesAndTerms(pool) {
  const [rules, terms] = await Promise.all([pool.rules(), pool.terms()]);
  const assets = await assetNames(rules.assets);
  return { rules, terms, assets };
}

export function termsHtml(terms) {
  return [
    row("Challenge price", `${chain.usd6(terms.price)} USDC`),
    row("Challenge capital", `${chain.usd6(terms.capital)} USDC`),
    row("Profit target", pct(terms.targetBps)),
    row("Time limit", esc(duration(terms.duration))),
    row("Trader's share of profit", pct(terms.traderShareBps)),
    row("Funded capital after passing", `${chain.usd6(terms.fundedCapital)} USDC`),
  ].join("");
}

export function rulesHtml(rules, assets) {
  return [
    row("Daily loss limit", pct(rules.dailyLossBps)),
    row("Max drawdown", pct(rules.maxDrawdownBps)),
    row("Max leverage", `${(Number(rules.maxLeverageX100) / 100).toFixed(2)}×`),
    row("Assets", esc(assets)),
  ].join("");
}

export async function listView(page) {
  if (!chain.deployed()) return notDeployed(page);
  render(page, `<section><h2>Pools</h2><p class="muted">Loading…</p><div id="pools" class="grid"></div></section>`);
  const addresses = await chain.factory().pools();
  const box = $("#pools", page);
  $(".muted", page).textContent = addresses.length
    ? `${addresses.length} pool(s). Each one sells a single challenge at a time.`
    : "No pools yet.";
  for (const address of [...addresses].reverse()) {
    const pool = chain.contract("pool", address);
    const [{ rules, terms, assets }, stage, owner, spotUsdc] = await Promise.all([
      rulesAndTerms(pool), pool.stage(), pool.owner(), hl.spotUsdc(address),
    ]);
    const open = Number(stage) === 0;
    const card = document.createElement("article");
    card.className = "card";
    card.innerHTML = `
      <h3><a href="#/pool/${esc(address)}">${esc(chain.short(address))}</a>
        ${badge(chain.STAGE[Number(stage)], open ? "ok" : "")}</h3>
      <p class="muted">Investor ${esc(chain.short(owner))} · ${esc(spotUsdc.toFixed(2))} USDC on HyperCore spot</p>
      ${termsHtml(terms)}
      ${rulesHtml(rules, assets)}
      <p><a class="button" href="#/pool/${esc(address)}">${open ? "Open the pool" : "See the pool"}</a></p>`;
    box.append(card);
  }
}

export async function newPoolView(page) {
  if (!chain.deployed()) return notDeployed(page);
  const list = await hl.perps();
  const listed = await Promise.all(list.slice(0, 64).map((p) => chain.factory().isPlatformAsset(p.index)));
  const options = list
    .slice(0, 64)
    .filter((_, i) => listed[i])
    .map((p) => `<label class="check"><input type="checkbox" name="asset" value="${p.index}" checked> ${esc(p.name)}</label>`)
    .join("");
  render(page, `
    <section class="card narrow">
      <h2>New pool</h2>
      <p class="muted">You set the rules and what a challenge costs. Your wallet becomes the pool's owner.
      Testnet, mock USDC.</p>
      <form id="pool-form">
        <fieldset><legend>Rules</legend>
          <label>Daily loss limit, % <input name="daily" type="number" step="0.1" min="0.1" max="99" value="5"></label>
          <label>Max drawdown from start, % <input name="dd" type="number" step="0.1" min="0.1" max="99" value="10"></label>
          <label>Max leverage, × <input name="lev" type="number" step="0.1" min="1" value="5"></label>
          <div class="checks">${options || "<em>No assets listed by the platform yet.</em>"}</div>
        </fieldset>
        <fieldset><legend>Challenge</legend>
          <label>Price, USDC <input name="price" type="number" step="0.01" min="0" value="25"></label>
          <label>Capital, USDC <input name="capital" type="number" step="1" min="11" value="100"></label>
          <label>Profit target, % <input name="target" type="number" step="0.1" min="0.1" value="8"></label>
          <label>Time limit, days <input name="days" type="number" step="1" min="1" value="7"></label>
          <label>Trader's share of profit, % <input name="share" type="number" step="1" min="0" max="100" value="80"></label>
          <label>Funded capital after passing, USDC <input name="funded" type="number" step="1" min="11" value="200"></label>
        </fieldset>
        <button type="button" id="create">Create the pool</button>
      </form>
    </section>`);

  wire($("#create", page), async () => {
    const form = $("#pool-form", page);
    // The button isn't a submit button, so the browser won't check min, max and step itself.
    if (!form.reportValidity()) throw new Error("Some fields are out of range; see the highlighted ones.");
    const f = new FormData(form);
    const bps = (name) => Math.round(Number(f.get(name)) * 100);
    const assets = f.getAll("asset").map(Number);
    if (!assets.length) throw new Error("Pick at least one asset.");
    const rules = {
      dailyLossBps: bps("daily"),
      maxDrawdownBps: bps("dd"),
      maxLeverageX100: Math.round(Number(f.get("lev")) * 100),
      assets,
    };
    const terms = {
      price: chain.toUnits(f.get("price"), 6),
      capital: chain.toUnits(f.get("capital"), 6),
      targetBps: bps("target"),
      duration: Number(f.get("days")) * 86400,
      traderShareBps: bps("share"),
      fundedCapital: chain.toUnits(f.get("funded"), 6),
    };
    const receipt = await chain.write("factory", chain.factory().target, "createPool", [rules, terms]);
    const log = receipt.logs
      .map((l) => { try { return chain.factory().interface.parseLog(l); } catch { return null; } })
      .find((l) => l && l.name === "PoolCreated");
    if (log) location.hash = `#/pool/${log.args.pool}`;
    return "Pool created.";
  });
}
