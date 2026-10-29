// The list of pools, and the form that creates one.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { esc, render, $, wire, pct, duration, badge, row, friendly } from "../lib/ui.js";
import { minPrice, gridText } from "../lib/floor.js";
import { DEMO_POOL as D } from "../lib/demo.js";
import { poolStatus } from "../lib/stages.js";
import { saleBlocker } from "../lib/funding.js";
import { totalPriceWords } from "../lib/outcomes.js";
import { DEPLOYMENTS, liveDeployment, isArchive, ARCHIVE, poolRules, defaultCapsText, capsByAsset } from "../lib/deployments.js";
import { LANDING, OTHERS_HEADING, KIND_NOTE, LIST_KEPT, readAgo, poolKind, holdsCode, cardBlockerLine, listOrder, tradeTarget }
  from "../lib/listing.js";
import { agentTradability, agentCardLine, agentFormWarning } from "../lib/agentcap.js";

/** The testnet line over the list and over a pool's page. */
export const TESTNET_LINE = `<p class="testnet-line">Testnet. Mock USDC. Unaudited.</p>`;

/** One figure of a pool with its label: a cell of a list row, and of the row at the top of a pool's page. */
export function keyCell(label, value) {
  return `<span class="cell"><span class="cell-label">${esc(label)}</span>
    <span class="cell-value">${value}</span></span>`;
}

function notDeployed(page) {
  render(page, `<section class="card"><h2>Not deployed yet</h2>
    <p>The contracts are not on testnet yet, so there is nothing to list. The addresses go into
    <code>app/config.js</code> after deployment.</p></section>`);
}

export async function assetNames(indices) {
  const names = await Promise.all(indices.map((i) => hl.perp(Number(i)).then((p) => p.name)));
  return names.join(", ");
}

export async function rulesAndTerms(pool, deployment = null) {
  const [rules, terms, caps] = await Promise.all([pool.rules(), pool.terms(), chain.assetCaps(pool.target, deployment)]);
  const assets = await assetNames(rules.assets);
  return { rules, terms, assets, caps };
}

/** The pool's terms. With the factory's fee, the price row is followed by what a buyer pays in all. */
export function termsHtml(terms, fee = null) {
  return [
    row("Challenge price", `${chain.usd6(terms.price)} USDC`),
    ...(fee === null ? [] : [row("You pay in total", esc(totalPriceWords(terms.price, fee)))]),
    row("Challenge capital", `${chain.usd6(terms.capital)} USDC`),
    row("Profit target", pct(terms.targetBps)),
    row("Time limit", esc(duration(terms.duration))),
    row("Trader's share of the challenge profit", pct(terms.traderShareChallengeBps)),
    row("Trader's share of the funded profit", pct(terms.traderShareFundedBps)),
    row("Funded capital after passing", `${chain.usd6(terms.fundedCapital)} USDC`),
  ].join("");
}

export function rulesHtml(rules, assets, caps = []) {
  return [
    row("Daily loss limit", pct(rules.dailyLossBps)),
    row("Max drawdown", pct(rules.maxDrawdownBps)),
    row("Max leverage", `${(Number(rules.maxLeverageX100) / 100).toFixed(2)}×`),
    // A v3 account's own cap for each asset (AUDIT В6-2); none before v3.
    ...(caps.length ? [row("Max leverage by asset", esc(capsByAsset(assets, caps)))] : []),
    row("Assets", esc(assets)),
  ].join("");
}

/**
 * How many pools the list reads at one time. Their reads ride in packs of fifty (app/lib/gentle.js), so thirty
 * pools, seven reads each, are five calls in one request to the node; a deployment with more is read in turns.
 */
export const POOLS_AT_ONCE = 30;

/**
 * One pool as the chain gives it, read from the deployment it belongs to. Its balance on HyperCore is asked of
 * Hyperliquid at the same moment and is not waited for here: the list shows the pool, and the balance when it comes.
 */
async function readPoolChain(address, index, deployment) {
  const pool = chain.contract("pool", address);
  const archived = isArchive(deployment);
  const spot = hl.spotUsdc(address);
  spot.catch(() => {});   // it is awaited in withSpot, which is where its failure is said
  // An archived pool sells nothing, so what would stop a sale is not asked of it: three reads a pool that the card
  // of an archived pool never showed.
  const sale = archived ? [null, null, 0n] : [pool.accountReady(), pool.challenge(), pool.capitalNeeded()];
  // Whether the owner is a contract is asked the moment the owner is known, beside the names of the assets, which
  // are Hyperliquid's: asked after them, it waited for an answer it does not need.
  const owned = pool.owner();
  const [{ rules, terms, assets, caps }, stage, owner, ownerIsContract, ready, challenge, neededSpot] = await Promise.all([
    rulesAndTerms(pool, deployment), pool.stage(), owned,
    owned.then((address) => holdsCode((a) => chain.readProvider.getCode(a), address)), ...sale,
  ]);
  return { index, address, rules, terms, assets, caps, stage, owner, archived, deployment, ready, challenge, neededSpot, spot,
    spotUsdc: null, blocker: archived ? { kind: "archived" } : undefined,
    kind: poolKind({ owner, ownerIsContract, deployer: deployment.deployer, address,
      liveRunPool: deployment.liveRunPool }) };
}

/**
 * The same pool once Hyperliquid has said what it holds: now it is known whether it can sell. When Hyperliquid
 * does not answer for a pool, that pool stays as the chain gave it, marked `unread`: its card says so, and the
 * list that is already on screen is not lost to one balance.
 */
async function withSpot(item) {
  let spotUsdc;
  try {
    spotUsdc = await item.spot;
  } catch {
    return { ...item, spotUsdc: null, unread: true };
  }
  // The same reading the pool page decides its buy button from (funding.js), in the same units. An archived pool
  // sells nothing, so it carries no blocker of its own: the deployment is the reason.
  const blocker = item.archived ? { kind: "archived" }
    : saleBlocker({ stage: item.stage, ready: item.ready, challenge: item.challenge, spot: spotUsdc,
      needed: Number(item.neededSpot) / 1e8 });
  return { ...item, spotUsdc, blocker };
}

/** A pool's card. Until its balance is known (`spotUsdc` null), and when it could not be read, the card says so and
 *  gives no verdict on a sale. */
function poolCard(item, fee) {
  const { address, rules, terms, assets, stage, owner, spotUsdc, blocker, kind, archived } = item;
  const known = spotUsdc !== null;
  const status = known || archived ? poolStatus(stage, archived ? null : blocker, archived) : null;
  const card = document.createElement("article");
  card.className = "card";
  const blocked = archived || !known ? "" : cardBlockerLine(blocker);
  // An archived pool sells nothing, so what an agent could do with its challenge is not a question there.
  const agent = archived ? "" : agentCardLine(agentTradability({ capital: Number(terms.capital) / 1e6,
    fundedCapital: Number(terms.fundedCapital) / 1e6, leverageX100: rules.maxLeverageX100 }));
  const held = known ? `${esc(spotUsdc.toFixed(2))} USDC on HyperCore spot`
    : esc(item.unread ? LIST_KEPT.unread : LIST_KEPT.balance);
  // One row a pool: the figures a visitor compares pools by, with the same labels the card has always had. The rest
  // of the card opens under it, word for word as before.
  card.className = "card pool-row";
  card.dataset.search = `${address} ${owner} ${assets}`.toLowerCase();
  card.dataset.status = status ? status.name : "";
  card.dataset.dd = String(Number(rules.maxDrawdownBps));
  card.dataset.target = String(Number(terms.targetBps));
  card.dataset.price = String(Number(terms.price));
  card.dataset.funded = String(Number(terms.fundedCapital));
  card.dataset.held = String(known ? spotUsdc : -1);
  card.dataset.order = String(item.index);
  const cell = keyCell;
  card.innerHTML = `
    <div class="pool-line">
    <h3><a href="#/pool/${esc(address)}">${esc(chain.short(address))}</a> ${status ? badge(status.name, status.tone) : ""}</h3>
    ${cell("Profit target", pct(terms.targetBps))}
    ${cell("Challenge price", `${chain.usd6(terms.price)} USDC`)}
    ${cell("Funded capital after passing", `${chain.usd6(terms.fundedCapital)} USDC`)}
    ${cell("Max drawdown", pct(rules.maxDrawdownBps))}
    ${cell("HyperCore spot", known ? `${esc(spotUsdc.toFixed(2))} USDC` : "…")}
    <p class="pool-open"><a class="button" href="#/pool/${esc(address)}">${blocker || !known ? "See the pool" : "Open the pool"}</a></p>
    </div>
    <details class="pool-more"><summary>Rules and terms</summary>
    ${KIND_NOTE[kind] ? `<p class="small"><strong>${esc(KIND_NOTE[kind])}</strong></p>` : ""}
    <p class="muted">Investor ${esc(chain.short(owner))} · ${held}</p>
    ${blocked ? `<p class="small">${esc(blocked)}</p>` : ""}
    ${agent ? `<p class="small">${esc(agent)}</p>` : ""}
    ${termsHtml(terms, archived ? null : fee)}
    ${rulesHtml(rules, assets, item.caps)}
    </details>`;
  return card;
}

// The list as it was last read in full, kept while the tab is open. Coming back to this page shows it at once, says
// how long ago it was read, and reads the chain again: the page is never empty for the seconds a reading takes.
let lastList = null;   // { at, items, fee }

/**
 * Both deployments at once, live first in the result. `seen(items, fee)` is told what the chain has said so far,
 * before Hyperliquid's balances are in.
 */
async function readList(live, seen = () => {}) {
  const sofar = new Map();
  const parts = await Promise.all(DEPLOYMENTS.filter((d) => chain.deployed(d)).map(async (deployment) => {
    const factory = chain.factory(undefined, deployment);
    const [addresses, fee] = await Promise.all([factory.pools(), factory.challengeFee()]);
    // In the factory's order. Their reads leave together, so the reader under the provider sends them as a pack or
    // two (app/lib/gentle.js), where a pool at a time cost a request for every pool.
    const fromChain = await chain.readAll([...addresses.entries()],
      ([index, address]) => readPoolChain(address, index, deployment), POOLS_AT_ONCE);
    sofar.set(deployment, { items: fromChain, fee });
    seen(DEPLOYMENTS.flatMap((d) => sofar.get(d)?.items ?? []), sofar.get(live)?.fee ?? 0n);
    return { deployment, fee, items: await Promise.all(fromChain.map(withSpot)) };
  }));
  return { items: parts.flatMap((part) => part.items), fee: parts.find((part) => part.deployment === live)?.fee ?? 0n };
}

/** The list on the page: `complete` once every balance is in, and only then does "I want to trade" lead anywhere. */
function paint(page, { items, fee }, complete) {
  const liveItems = items.filter((i) => !i.archived);
  $("#count", page).textContent = liveItems.length
    ? `${liveItems.length} pool(s) on the live deployment. Each one sells a single challenge at a time.`
    : "No pools on the live deployment yet.";
  const ordered = listOrder(liveItems);
  $("#pools", page).replaceChildren(...ordered.filter((i) => i.kind === "pool").map((i) => poolCard(i, fee)));
  $("#others", page).replaceChildren(...ordered.filter((i) => i.kind !== "pool").map((i) => poolCard(i, fee)));
  $("#others-heading", page).hidden = !ordered.some((i) => i.kind !== "pool");
  const archived = listOrder(items.filter((i) => i.archived));
  $("#archive", page).replaceChildren(...archived.map((i) => poolCard(i, fee)));
  $("#archive-block", page).hidden = !archived.length;
  listTools(page);
  if (!complete) return;
  // A pool whose balance could not be read is not one to send a trader to: whether it can sell is not known.
  const target = tradeTarget(ordered.filter((i) => i.spotUsdc !== null));
  const trade = $("#trade-in", page);
  trade.removeAttribute("aria-disabled");
  trade.href = target ? `#/pool/${target}` : "#/";
  // Honestly to the list: when no investor's pool of the live deployment can sell, the button shows the list, and
  // our benches are not an offer.
  trade.onclick = target ? null : (e) => {
    e.preventDefault();
    $("#count", page).scrollIntoView({ behavior: "smooth" });
  };
  $("#trade-none", page).textContent = target ? "" : LANDING.trade.none;
}

/** How many pools one page of the list shows. */
export const PER_PAGE = 20;

/**
 * Search, filters, sorting and pages over the cards already on the page. Nothing is read again: the cards carry
 * what they are filtered and sorted by, and the ones left out are only hidden.
 */
function listTools(page) {
  const form = $("#list-tools", page);
  if (!form) return;
  form.onsubmit = (e) => e.preventDefault();
  const grid = $("#pools", page);
  // Each block of the list is filtered on its own, and only the live pools are counted and paged, so the counter,
  // the pages and what is shown speak of the same cards. A block the filters leave empty hides with its heading.
  const blocks = [[$("#others", page), $("#others-heading", page)], [$("#archive", page), $("#archive-block", page)]]
    .filter(([box]) => box);
  const cards = [grid, ...blocks.map(([box]) => box)].flatMap((box) => [...box.querySelectorAll(".pool-row")]);
  const fill = (select, values, label) => {
    const was = select.value;
    select.replaceChildren(new Option("All", ""), ...values.map((v) => new Option(label(v), v)));
    select.value = values.includes(was) ? was : "";
  };
  fill(form.elements.status, [...new Set(cards.map((c) => c.dataset.status).filter(Boolean))].sort(), (v) => v);
  fill(form.elements.dd, [...new Set(cards.map((c) => c.dataset.dd))].sort((a, b) => a - b), (v) => pct(v));
  let at = 0;
  $("#pager", page).replaceChildren();
  const show = () => {
    const f = new FormData(form);
    const q = String(f.get("q") || "").trim().toLowerCase();
    const [key, way] = String(f.get("sort") || "order-desc").split("-");
    const fits = (c) => (!q || c.dataset.search.includes(q)) && (!f.get("status") || c.dataset.status === f.get("status"))
      && (!f.get("dd") || c.dataset.dd === f.get("dd"));
    for (const c of cards) c.hidden = !fits(c);
    for (const [box, head] of blocks) {
      if (box.children.length) head.hidden = ![...box.children].some((c) => !c.hidden);
    }
    const mine = [...grid.children].filter((c) => !c.hidden)
      .sort((a, b) => (way === "asc" ? 1 : -1) * (Number(a.dataset[key]) - Number(b.dataset[key])));
    const pages = Math.max(1, Math.ceil(mine.length / PER_PAGE));
    at = Math.min(at, pages - 1);
    grid.replaceChildren(...mine, ...[...grid.children].filter((c) => c.hidden));
    mine.forEach((c, i) => { c.hidden = Math.floor(i / PER_PAGE) !== at; });
    const all = [...grid.children].length;
    $("#shown", page).textContent = mine.length
      ? `${at * PER_PAGE + 1} to ${Math.min(mine.length, (at + 1) * PER_PAGE)} of ${mine.length} (${all} in all)`
      : `0 of ${all}`;
    const pager = $("#pager", page);
    // Same number of pages: only the current mark moves, so the button in focus keeps it.
    if (pages > 1 && pager.children.length === pages) {
      [...pager.children].forEach((b, i) => {
        if (i === at) b.setAttribute("aria-current", "page");
        else b.removeAttribute("aria-current");
      });
      return;
    }
    pager.replaceChildren(...(pages > 1 ? Array.from({ length: pages }, (_, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = String(i + 1);
      if (i === at) b.setAttribute("aria-current", "page");
      b.onclick = () => { at = i; show(); grid.scrollIntoView({ block: "start" }); };
      return b;
    }) : []));
  };
  form.oninput = () => { at = 0; show(); };
  show();
}

export async function listView(page) {
  const live = liveDeployment();
  if (!chain.deployed(live)) return notDeployed(page);
  render(page, `<section>
    <h2>Pools</h2>
    ${TESTNET_LINE}
    <p class="lead">${esc(LANDING.line)}</p>
    <p class="actions"><a class="button" href="${esc(LANDING.invest.href)}">${esc(LANDING.invest.label)}</a>
      <a class="button" id="trade-in" aria-disabled="true">${esc(LANDING.trade.label)}</a>
      <span class="small muted" id="trade-none"></span></p>
    <p class="muted" id="count">Loading…</p>
    <p class="small muted" id="as-read" hidden></p>
    <form class="list-tools" id="list-tools" role="search">
      <label>Search <input type="search" name="q" placeholder="Address or asset"></label>
      <label>Status <select name="status"><option value="">All</option></select></label>
      <label>Max drawdown <select name="dd"><option value="">All</option></select></label>
      <label>Sort <select name="sort">
        <option value="order-desc">Newest</option>
        <option value="target-desc">Profit target, high first</option>
        <option value="price-asc">Challenge price, low first</option>
        <option value="funded-desc">Funded capital, high first</option>
        <option value="dd-asc">Max drawdown, low first</option>
        <option value="held-desc">HyperCore spot, high first</option>
      </select></label>
    </form>
    <p class="small muted" id="shown"></p>
    <div id="pools" class="grid list"></div>
    <nav class="pager" id="pager" aria-label="Pages"></nav>
    <h3 id="others-heading" hidden>${esc(OTHERS_HEADING)}</h3>
    <div id="others" class="grid list"></div>
    <div id="archive-block" hidden>
      <h3>${esc(ARCHIVE.heading)}</h3>
      <p class="muted">${esc(ARCHIVE.note)}</p>
      <div id="archive" class="grid list"></div>
    </div></section>`);
  const kept = lastList;
  const asRead = $("#as-read", page);
  // Every card names its assets from Hyperliquid's list of perps: asked now, it is in by the time the pools are.
  hl.perps().catch(() => {});
  if (kept) {
    // What was read before, at once, and how old it is. It stays until the new reading is whole.
    paint(page, kept, true);
    asRead.textContent = `${LIST_KEPT.shown(readAgo(Date.now() - kept.at))} ${LIST_KEPT.again}`;
    asRead.hidden = false;
  }
  try {
    // With nothing kept, the pools are shown as soon as the chain has said what they are.
    const read = await readList(live, kept ? undefined : (items, fee) => paint(page, { items, fee }, false));
    lastList = { ...read, at: Date.now() };
    paint(page, lastList, true);
    asRead.hidden = true;
  } catch (err) {
    if (!kept) throw err;
    // The list on screen is still what was read before: say that, and that the new reading failed.
    asRead.textContent = `${LIST_KEPT.shown(readAgo(Date.now() - kept.at))} ${LIST_KEPT.failed} ${friendly(err)}`;
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
  // Each listed perp by its own dex's name, as the gateway names it: a HIP-3 perp is not in Hyperliquid's own list,
  // and named "#750000" it would take no default cap of its own (AUDIT V7-2).
  const listedNames = new Map((await Promise.all(candidates.filter((_, i) => listed[i]).map((index) => hl.perp(Number(index)))))
    .map((p) => [Number(p.index), p.name]));
  const nameOf = (index) => listedNames.get(Number(index)) ?? list.find((p) => Number(p.index) === Number(index))?.name ?? `#${index}`;
  const options = candidates
    .filter((_, i) => listed[i])
    .map((index) => `<label class="check"><input type="checkbox" name="asset" value="${esc(index)}" checked>
        ${esc(nameOf(index))}</label>`)
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
          <label>Max leverage, × <input name="lev" type="number" step="0.1" min="1" max="10" value="${D.lev}"></label>
          <div class="checks">${options || "<em>No assets listed by the platform yet.</em>"}</div>
          ${liveDeployment()?.assetLeverage ? `<p class="small" id="asset-caps">Each asset also has its own leverage cap,
          never over the max leverage above: ${esc(defaultCapsText(candidates.filter((_, i) => listed[i]).map(nameOf)))}.
          Any other asset takes the max leverage.</p>` : ""}
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
        <p class="small" id="agent-warning"></p>
        <button type="button" id="create">Create the pool</button>
      </form>
      <p class="small" id="floor"></p>
    </section>`);

  // One line from the model, and only this one: what a challenge of THIS pool costs the pool.
  // The rest of the calculator is on the Economics page, because it describes a pool of several
  // seats with a shared reserve, which is not what this button makes.
  priceFloor(page).catch(() => { $("#floor", page).textContent = ""; });
  agentWarning(page);

  wire($("#create", page), async () => {
    const form = $("#pool-form", page);
    // The button isn't a submit button, so the browser won't check min, max and step itself.
    if (!form.reportValidity()) throw new Error("Some fields are out of range; see the highlighted ones.");
    const f = new FormData(form);
    const bps = (name) => Math.round(Number(f.get(name)) * 100);
    const assets = f.getAll("asset").map(Number);
    if (!assets.length) throw new Error("Pick at least one asset.");
    // Four fields for a factory before v3; on a v3 one, each asset's own cap after them.
    const rules = poolRules(liveDeployment(), {
      dailyLossBps: bps("daily"),
      maxDrawdownBps: bps("dd"),
      maxLeverageX100: Math.round(Number(f.get("lev")) * 100),
      assets,
    }, assets.map(nameOf));
    const terms = {
      price: chain.toUnits(f.get("price"), 6),
      capital: chain.toUnits(f.get("capital"), 6),
      targetBps: bps("target"),
      duration: Number(f.get("days")) * 86400,
      traderShareChallengeBps: bps("challengeShare"),
      traderShareFundedBps: bps("fundedShare"),
      fundedCapital: chain.toUnits(f.get("funded"), 6),
    };
    const receipt = await chain.write(chain.factoryKind(), chain.factory().target, "createPool", [rules, terms]);
    const log = receipt.logs
      .map((l) => { try { return chain.factory().interface.parseLog(l); } catch { return null; } })
      .find((l) => l && l.name === "PoolCreated");
    if (log) location.hash = `#/pool/${log.args.pool}`;
    return "Pool created.";
  });
}

/**
 * Above the button, before anything is sent: whether an agent could trade the pool this form would create through
 * the repository's client (lib/agentcap.js). A warning and not a refusal: the contract takes such a pool, and a
 * person can trade it from this site.
 */
function agentWarning(page) {
  const box = $("#agent-warning", page);
  const form = $("#pool-form", page);
  if (!box || !form) return;
  const show = () => {
    const f = new FormData(form);
    const leverageX100 = Math.round(Number(f.get("lev")) * 100);
    const words = agentFormWarning(agentTradability({ capital: Number(f.get("capital")),
      fundedCapital: Number(f.get("funded")), leverageX100 }), leverageX100);
    box.innerHTML = words ? `${badge("an agent could not trade this pool", "bad")} ${esc(words)}` : "";
  };
  form.addEventListener("input", show);
  show();
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
