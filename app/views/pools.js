// The list of pools, and the form that creates one.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { esc, render, $, wire, pct, duration, badge, row } from "../lib/ui.js";
import { minPrice, gridText } from "../lib/floor.js";
import { DEMO_POOL as D } from "../lib/demo.js";

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
    row("Trader's share of the challenge profit", pct(terms.traderShareChallengeBps)),
    row("Trader's share of the funded profit", pct(terms.traderShareFundedBps)),
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
  // The candidates come from the deployment record (app/config.js), the answer from the chain.
  // This used to ask isPlatformAsset for the first 64 perps in one Promise.all, which ethers
  // sends as one JSON-RPC batch -- and the public RPC refuses a batch over 20 calls outright,
  // so this page did not load at all. Measured 20 Sep 2026: -32010, "Exceeded max limit of 20".
  const [list, candidates] = [await hl.perps(), chain.platformAssets()];
  const listed = await chain.readAll(candidates, (index) => chain.factory().isPlatformAsset(index));
  const options = candidates
    .filter((_, i) => listed[i])
    .map((index) => {
      const perp = list.find((p) => Number(p.index) === Number(index));
      return `<label class="check"><input type="checkbox" name="asset" value="${esc(index)}" checked>
        ${esc(perp ? perp.name : `#${index}`)}</label>`;
    })
    .join("");
  render(page, `
    <section class="card narrow">
      <h2>New pool</h2>
      <p class="muted">You set the rules and what a challenge costs. Your wallet becomes the pool's owner.
      Testnet, mock USDC.</p>
      <form id="pool-form">
        <fieldset><legend>Rules</legend>
          <label>Daily loss limit, % <input name="daily" type="number" step="0.1" min="0.1" max="99" value="${D.daily}"></label>
          <label>Max drawdown from start, % <input name="dd" type="number" step="0.1" min="0.1" max="99" value="${D.dd}"></label>
          <label>Max leverage, × <input name="lev" type="number" step="0.1" min="1" value="${D.lev}"></label>
          <div class="checks">${options || "<em>No assets listed by the platform yet.</em>"}</div>
        </fieldset>
        <fieldset><legend>Challenge</legend>
          <label>Price, USDC <input name="price" type="number" step="0.01" min="0.01" value="${D.price}"></label>
          <label>Capital, USDC <input name="capital" type="number" step="1" min="11" value="${D.capital}"></label>
          <label>Profit target, % <input name="target" type="number" step="0.1" min="0.1" value="${D.target}"></label>
          <label>Time limit, days <input name="days" type="number" step="1" min="1" value="${D.days}"></label>
          <label>Trader's share of the challenge profit, %
            <input name="challengeShare" type="number" step="1" min="0" max="100" value="${D.challengeShare}"></label>
          <label>Trader's share of the funded profit, %
            <input name="fundedShare" type="number" step="1" min="0" max="100" value="${D.fundedShare}"></label>
          <label>Funded capital after passing, USDC <input name="funded" type="number" step="1" min="11" value="${D.funded}"></label>
        </fieldset>
        <button type="button" id="create">Create the pool</button>
      </form>
      <p class="small" id="floor"></p>
    </section>`);

  // One line from the model, and only this one: what a challenge of THIS pool costs the pool.
  // The rest of the calculator is on the Economics page, because it describes a pool of several
  // seats with a shared reserve, which is not what this button makes.
  priceFloor(page).catch(() => { $("#floor", page).textContent = ""; });

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
      traderShareChallengeBps: bps("challengeShare"),
      traderShareFundedBps: bps("fundedShare"),
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

/**
 * The floor under the challenge price, for the pool this form would create. The contract refuses a
 * price of zero and nothing more: what a challenge actually costs depends on the rules and on who
 * buys, which no contract can see. This says it before the pool exists, and says nothing when the
 * model has no cell for the rules chosen -- a floor shown to an investor has to be the floor of
 * their own rules, not of the nearest ones the model happens to hold.
 */
async function priceFloor(page) {
  const box = $("#floor", page);
  const form = $("#pool-form", page);
  if (!box || !form) return;
  const tables = await (await fetch("./data/calc_tables.json")).json();
  const show = () => {
    const f = new FormData(form);
    const terms = {
      fundedCapital: Number(f.get("funded")),
      capital: Number(f.get("capital")),
      dd: Number(f.get("dd")) / 100,
      daily: Number(f.get("daily")) / 100,
      target: Number(f.get("target")) / 100,
      share: Number(f.get("share") ?? f.get("fundedShare") ?? 80) / 100,
    };
    const floor = minPrice(tables, terms);
    if (floor.offGrid) {
      box.innerHTML = `<span class="muted">The model has no figure for these rules, so there is no cost line
        here. It holds ${esc(gridText(tables))}.</span>`;
      return;
    }
    if (floor.offRatio !== undefined) {
      // No figure and no "below cost" badge: the model describes a pool with a different challenge
      // account, and its cost is not this pool's.
      box.innerHTML = `<span class="muted">The model prices a challenge account at a tenth of the funded
        capital; this one is ${esc((floor.offRatio * 100).toFixed(0))}% of it. That is not the pool the model
        describes, so there is no cost line for it here rather than one for a different pool.</span>`;
      return;
    }
    const price = Number(f.get("price"));
    const under = price > 0 && price < floor.price;
    box.innerHTML = `${under ? badge("the price is below what a challenge costs this pool", "bad") : ""}
      By the model, one challenge of this pool costs it about
      <strong>${floor.price.toFixed(2)} USDC</strong>; sell below that and the investor pays for each sale.
      <span class="muted">Model, not a measurement — the whole of it is on the
      <a href="#/economics">Economics</a> page.</span>`;
  };
  form.addEventListener("input", show);
  show();
}
