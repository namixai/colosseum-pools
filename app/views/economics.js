// The Economics page: what a pool of this design earns and pays at product scale.
//
// It lives on its own page on purpose. The calculator describes a pool of several seats with a
// shared reserve, and the button on the "new pool" form creates a single-seat pool on testnet
// with mock USDC. Put the two next to each other and the layout says the numbers describe the
// pool you are about to make, which they do not -- and a footnote does not undo a layout. The
// one thing that does travel to that form is the minimum price for the pool actually being
// created, which is a fact about that pool.
import { evaluate, defaultLayout, SCENARIOS, cellKey } from "../lib/calc.js";
import { esc, render, $, badge, row } from "../lib/ui.js";

const SIZES = [100_000, 500_000, 1_000_000];
const MIN_POOL = 100_000;
/** More seats than any pool here would hold; erlangB walks every one of them, three times. */
export const MAX_SEATS = 1000;
const SCENARIO_NAME = { bad: "bad", base: "base", good: "good" };

let tables = null;

/**
 * The rule values the table has cells for, read from the table itself. The form offers these and
 * nothing else: the model refuses a rule off its grid rather than guessing one, and a form that
 * let you pick 6.5% would only turn that refusal into an error message.
 */
export function gridValues(tbl) {
  const seen = { dd: new Set(), daily: new Set(), target: new Set() };
  for (const key of Object.keys(tbl.cells)) {
    const [, , dd, daily, target] = key.split("|");
    seen.dd.add(Number(dd)); seen.daily.add(Number(daily)); seen.target.add(Number(target));
  }
  const sorted = (s) => [...s].sort((a, b) => a - b).map((bps) => bps / 100);
  return { dd: sorted(seen.dd), daily: sorted(seen.daily), target: sorted(seen.target) };
}

/** Whether the model has a cell for these rules, without throwing to find out. */
export function onGrid(tbl, { scenario, mode, dd, daily, target }) {
  return Object.prototype.hasOwnProperty.call(tbl.cells, cellKey(scenario, mode, dd, daily, target));
}

const options = (values, chosen) => values
  .map((v) => `<option value="${v}"${v === chosen ? " selected" : ""}>${v}%</option>`).join("");

async function loadTables() {
  if (!tables) tables = await (await fetch("./data/calc_tables.json")).json();
  return tables;
}

const money = (v) => `$${Math.round(Number(v)).toLocaleString("en-US")}`;
const money2 = (v) => `$${Number(v).toFixed(2)}`;
const percent = (v, digits = 1) => `${(Number(v) * 100).toFixed(digits)}%`;

export async function economicsView(page) {
  render(page, `
    <section class="card">
      <h2>Economics</h2>
      <div id="risk" class="muted">…</div>
      <p>What a pool of this design earns, what a trader can earn on it, and what it costs to run one.</p>
      <p class="notice"><strong>Every number the calculator returns is a model, not a measurement.</strong> The
      mechanics come from the contracts in this repository; who buys a challenge, how often, and how far a stop
      overshoots are assumptions. Nobody has run this with real traders. The exceptions on this page are the
      levers an investor sets and the cascade further down: those are measurements of real crash days, and they
      are labelled as such.</p>
      <p class="muted">This describes the product at scale. The demo on this site is a single-seat pool on
      testnet holding mock USDC, and these numbers are not about it.</p>
    </section>
    <section class="card"><h3>The pool</h3><div id="form" class="muted">Loading the model…</div></section>
    <section class="card"><h3>What it comes to</h3><div id="out" class="muted">…</div></section>
    <section class="card">
      <h3>Who takes the loss</h3>
      <p>The capital in a pool is the investor's, so the investor takes the loss. A trader risks what a
      challenge costs and nothing more: the challenge account holds the pool's money, and a pass moves
      the pool's money too.</p>
      <div id="who" class="muted">…</div>
      <p class="small">Check the pieces rather than this page: the rules are in the contract, every stop
      and every pass is a transaction anyone can read, and the table these figures come from ships with
      the page — <a href="./data/calc_tables.json">calc_tables.json</a>.</p>
    </section>
    <section class="card">
      <h3>What the rules do not protect against</h3>
      <p>The daily loss line and the drawdown line hold on an ordinary bad day. A cascade is a
      different thing, and the block below is <strong>not part of the model above</strong>: it is a
      measurement of real crash days replayed through these rules. It carries no frequency, so it is
      never subtracted from the returns. It is the stop <strong>with the keeper alone, a minute late</strong>, as
      pools ran before 29 Sep 2026: with the stop on the exchange, the same day stays at the line (the levers above).</p>
      <div id="cascade" class="muted">…</div>
    </section>`);
  await loadTables();
  $("#risk", page).className = "";
  $("#risk", page).innerHTML = riskSummary(tables);
  $("#who", page).className = "";
  $("#who", page).innerHTML = leversCard(tables);
  formPanel(page);
}

function formPanel(page) {
  const grid = gridValues(tables);
  $("#form", page).className = "";
  $("#form", page).innerHTML = `
    <form id="calc">
      <fieldset>
        <legend>How to set it up</legend>
        <label><input type="radio" name="mode" value="default" checked> Default — pick a size, we lay out the seats</label>
        <label><input type="radio" name="mode" value="custom"> Custom — set the seats and the terms yourself</label>
      </fieldset>
      <fieldset id="defaults">
        <legend>Size</legend>
        <label>Pool capital, $
          <input name="pool" type="number" step="10000" min="${MIN_POOL}" value="100000" list="sizes"></label>
        <datalist id="sizes">${SIZES.map((s) => `<option value="${s}">`).join("")}</datalist>
        <p class="small muted" id="layout"></p>
      </fieldset>
      <fieldset id="customs" hidden>
        <legend>Seats and terms</legend>
        <label>Seats, as <code>size×count</code>, comma separated
          <input name="seats" type="text" value="50000x1, 25000x1, 5000x3"></label>
        <label>Reserve kept back, $ <input name="reserve" type="number" step="1000" min="0" value="10000"></label>
        <label>Trader's share of the funded profit, %
          <input name="share" type="number" step="1" min="0" max="100" value="80"></label>
        <label>Challenge price, % of the seat
          <input name="pricePct" type="number" step="0.05" min="0.01" value="1"></label>
        <label>Max drawdown <select name="dd">${options(grid.dd, 6)}</select></label>
        <label>Daily loss limit <select name="daily">${options(grid.daily, 3)}</select></label>
        <label>Profit target <select name="target">${options(grid.target, 10)}</select></label>
      </fieldset>
      <fieldset>
        <legend>The same for both</legend>
        <label>Assets the rules allow
          <select name="assetList">
            <option value="default">BTC, ETH, SOL — what the demo deploys with</option>
            <option value="wide">a wide list, ten coins including SUI and DOGE</option>
          </select></label>
        <label>Challenge account
          <select name="chmode">
            <option value="real">real — a tenth of the seat, traded for real</option>
            <option value="demo">paper — the pool's capital is not at risk until the trader passes</option>
          </select></label>
        <label>Platform's cut of the price, % <input name="feePct" type="number" step="5" min="0" max="100" value="20"></label>
      </fieldset>
    </form>
    <p class="small muted">The rules on offer are the ones the model has cells for. It refuses a rule off its
    grid rather than guessing one, so the lists above are short on purpose.</p>`;

  const form = $("#calc", page);
  const redraw = () => {
    const custom = form.elements.mode.value === "custom";
    $("#defaults", page).hidden = custom;
    $("#customs", page).hidden = !custom;
    run(form, page);
  };
  form.addEventListener("input", redraw);
  form.addEventListener("change", redraw);
  redraw();
}

/** "50000x1, 25000x1, 5000x3" -> [{F, count}]. A line that parses to nothing is an error, not an empty pool. */
export function parseSeats(text) {
  const seats = String(text).split(",").map((part) => {
    const m = part.trim().match(/^(\d+(?:\.\d+)?)\s*[x*×]\s*(\d+)$/i);
    if (!m) throw new Error(`"${part.trim()}" is not a seat: write it as size×count, for example 50000x1`);
    const F = Number(m[1]);
    const count = Number(m[2]);
    if (!(F > 0) || !(count > 0)) throw new Error(`"${part.trim()}" is not a seat: both numbers must be above zero`);
    if (!Number.isFinite(F) || !Number.isSafeInteger(count)) {
      throw new Error(`"${part.trim()}" is not a seat: the numbers are too large to be a pool`);
    }
    return { F, count };
  });
  if (!seats.length) throw new Error("Name at least one seat, for example 50000x1");
  return withinSeatCap(seats);
}

/**
 * The Erlang formula walks every seat of a group, for each of three scenarios, in the page's own
 * thread, on every keystroke. Too many seats would freeze the tab rather than fail -- whether they
 * were typed as a count with extra zeros or came from a pool size with extra zeros. Both paths to
 * seats come through here, so neither can forget the cap.
 */
export function withinSeatCap(seats) {
  const total = seats.reduce((a, s) => a + s.count, 0);
  if (!Number.isSafeInteger(total) || total > MAX_SEATS) {
    throw new Error(`${Number.isFinite(total) ? total.toLocaleString("en-US") : "that many"} seats is more `
      + `than this calculator takes (${MAX_SEATS})`);
  }
  return seats;
}

/** An empty field is zero; anything negative or not a number is refused before the division. */
export function reserveFrom(text) {
  const reserve = Number(text ?? 0);
  if (!Number.isFinite(reserve) || reserve < 0) {
    throw new Error("The reserve kept back has to be zero or more dollars");
  }
  return reserve;
}

export function specFrom(values) {
  const base = {
    asset_list: values.assetList || "default",
    mode: values.chmode,
    fee_pct_price: Number(values.feePct) / 100,
    price_pct: Number(values.pricePct ?? 1) / 100,
  };
  if (values.mode === "custom") {
    return {
      ...base,
      ...{ seats: parseSeats(values.seats), reserve: reserveFrom(values.reserve) },
      trader_share: Number(values.share) / 100,
      max_drawdown: Number(values.dd) / 100,
      daily_loss: Number(values.daily) / 100,
      target: Number(values.target) / 100,
    };
  }
  const pool = Number(values.pool);
  if (!(pool >= MIN_POOL)) {
    const err = new Error("below the minimum");
    err.tooSmall = true;
    throw err;
  }
  if (!Number.isFinite(pool)) throw new Error("The pool's capital has to be a number of dollars");
  const layout = defaultLayout(pool);
  withinSeatCap(layout.seats);
  return { ...base, ...layout, price_pct: 0.01,
    trader_share: 0.80, max_drawdown: 0.06, daily_loss: 0.03, target: 0.10 };
}

function run(form, page) {
  const values = Object.fromEntries(new FormData(form));
  const box = $("#out", page);
  const cascadeBox = $("#cascade", page);
  // Whatever this input turns out to be, the cascade of the previous one is not about it.
  const clearCascade = (why) => {
    cascadeBox.className = "muted";
    cascadeBox.textContent = why;
  };
  let spec;
  try {
    spec = specFrom(values);
  } catch (e) {
    box.className = "";
    box.innerHTML = e.tooSmall ? SHARED_POOL : `<p class="notice">${esc(e.message)}</p>`;
    clearCascade(e.tooSmall ? "No pool of its own below the minimum, so no cascade to show."
      : "Fix the pool above to see what a cascade does to it.");
    return;
  }
  if (values.mode === "default") {
    const layout = $("#layout", page);
    if (layout) {
      layout.textContent = `Seats: ${spec.seats.map((s) => `${s.count} × ${money(s.F)}`).join(", ")}`
        + `; reserve ${money(spec.reserve)}.`;
    }
  }
  let runs;
  try {
    runs = SCENARIOS.map((scenario) => [scenario, evaluate(tables, { ...spec, scenario })]);
  } catch (e) {
    box.className = "";
    box.innerHTML = `<p class="notice">${esc(String(e.message || e))}</p>`;
    clearCascade("Fix the pool above to see what a cascade does to it.");
    return;
  }
  box.className = "";
  box.innerHTML = results(Object.fromEntries(runs));
  cascadeBox.className = "";
  cascadeBox.innerHTML = cascade(Object.fromEntries(runs).base);
}

/** A share already in percent, as the lever cells carry it. */
const points = (v, digits = 0) => `${Number(v).toFixed(digits)}%`;

/**
 * One cell of the lever table: where the stop sits ("exchange" at the line, or "keeper" a minute
 * late), the leverage, the list ("default" or "wide") and, for the keeper, which end of its minute.
 */
export function leverCell(tables, stop, leverage, list, exec = "open") {
  const cell = tables.levers.cells.find((c) => c.stop === stop && c.leverage === leverage && c.list === list
    && c.exec === exec);
  if (!cell) throw new Error(`no lever cell for ${stop} ${leverage}x ${list} ${exec}`);
  return cell;
}

/**
 * A stop on the exchange closes at the line; walking the book adds a little on top, measured only in a
 * calm market. The worst day's bracket: the line, plus at the low end half the spread of the calmest
 * coin, and at the high end the worst hour's walk, the highest over every seat and coin, each seat priced
 * at the nearest measured size at or above its notional. Wider than the truth on purpose: a seat sits on
 * one coin, not on all of them. Basis points are of the notional, so they count times the leverage
 * against the seat's own capital.
 */
export function exchangeBracket(tables, leverage) {
  const book = tables.levers.book;
  const line = Math.max(...["default", "wide"].map((list) => leverCell(tables, "exchange", leverage, list).worst_pct));
  const bound = (coin, notional) => {
    const sizes = Object.keys(book.walk_bps[coin]).map(Number).sort((a, b) => a - b);
    return book.walk_bps[coin][String(sizes.find((z) => z >= notional) ?? sizes[sizes.length - 1])];
  };
  const low = Math.min(...Object.values(book.half_spread_bps)) * leverage / 100;
  const high = Math.max(...book.seats_usd.flatMap((seat) => Object.keys(book.walk_bps)
    .map((coin) => bound(coin, seat * leverage).p99))) * leverage / 100;
  return { line, low: line + low, high: line + high };
}

/**
 * The risk in four sentences, at the top of the page: who can lose what, where the line runs between what was
 * checked on Hyperliquid and what was replayed on Bybit's data, and that the returns are a model. The two losses
 * are the lever table's, long side, 5x, the list with alts: the keeper a minute late at both ends of its minute,
 * and the stop on the exchange with the book walked on top (exchangeBracket).
 */
export function riskSummary(tbl) {
  const [open, worst] = ["open", "worst"].map((e) => leverCell(tbl, "keeper", 5, "wide", e));
  return `<p class="lead"><strong>A pool can lose its investor's capital; a trader can lose only what a challenge
    costs.</strong> Checked on Hyperliquid testnet, and readable on chain: the mechanism — the gateway puts a stop and
    a take on the exchange before any order that may open a position, and the contract enforces the rules and records
    every stop. Replayed on Bybit's one-minute data, not measured on Hyperliquid: the losses — on the worst of the
    ${tbl.levers.days.length} crash days, long side, at 5x on a list with alts, a stop on the exchange cost at most
    ${points(exchangeBracket(tbl, 5).high, 1)}, a stop a minute late ${points(open.worst_pct)} to
    ${points(worst.worst_pct)} of the seats' capital. No pool has run with real traders or real money, so every return
    on this page is a model.</p>`;
}

/**
 * Who takes the loss, and what the investor decides about it: the three layers that guard a seat, then
 * the levers with what each cost on the worst measured day, then the worst case, named as the case
 * without a stop on the exchange. Every figure is read from the table; a card that restates them in prose
 * drifts from the measurement the first time it is redone.
 */
export function leversCard(tables) {
  const L = tables.levers;
  const cell = (stop, lev, list, exec) => leverCell(tables, stop, lev, list, exec);
  const keeper = (lev, list) => {
    const [open, worst] = [cell("keeper", lev, list, "open"), cell("keeper", lev, list, "worst")];
    const [least, most] = [open, worst].map((c) => c.seats_liquidated_on_worst).sort((a, b) => a - b);
    return `${points(open.worst_pct)} to ${points(worst.worst_pct)}`
      + (most ? `, ${least === most ? "" : "up to "}${most} of 5 seats liquidated` : "");
  };
  const medians = L.cells.map((c) => c.median_of_days_pct);
  const day = cell("keeper", 5, "wide", "worst").worst_day;
  const [b3, b5] = [exchangeBracket(tables, 3), exchangeBracket(tables, 5)];
  const tail = [cell("keeper", 5, "wide", "open"), cell("keeper", 5, "wide", "worst")];
  const rows = [3, 5].map((lev) => `<tr><td>${lev}x</td>
      <td>${points(cell("exchange", lev, "default").worst_pct, 1)}</td>
      <td>${points(cell("exchange", lev, "wide").worst_pct, 1)}</td>
      <td>${keeper(lev, "default")}</td><td>${keeper(lev, "wide")}</td></tr>`).join("");
  return `<p>Three things guard a seat, one after another.</p>
    <ol>
      <li><strong>Before signing, the gateway.</strong> It keeps to the platform's list of assets and its size
      caps.</li>
      <li><strong>On the exchange, a stop at the rule line and a take at the target.</strong> The gateway puts
      them on Hyperliquid before any order that may open a position, so the exchange closes at the line
      without waiting for anyone. <em>On this site since 29 Sep 2026, after a live check on testnet through a
      gateway running the same code.</em></li>
      <li><strong>After the fact, the contract.</strong> Anyone may stop an account that broke a rule, and our
      keeper looks every 30 seconds; between the crossing and the stop landing, a loss can go past the line.</li>
    </ol>
    <p>The levers below are measured on the long side, as in the headline of the cascade package's README;
    the cascade further down shows the worse of the two sides on the same days.</p>
    <p>Where the stop sits is the investor's biggest lever, and two more sit beside it: the leverage the seats
    may take, and the coins they may trade. Replayed through these rules on one-minute bars of the eight worst
    crash days between ${esc([...L.days].sort()[0])} and ${esc([...L.days].sort().at(-1))}, here is what each
    choice cost on the worst of them, ${esc(day)}, as a share of the seats' capital — the investor's own
    money:</p>
    <div class="scroll"><table>
      <tr><th rowspan="2">Leverage</th><th colspan="2">Stop on the exchange, at the line<br>(this site, since 29 Sep 2026)</th>
        <th colspan="2">Stop with the keeper alone, a minute late<br>(before 29 Sep 2026)</th></tr>
      <tr><th>BTC, ETH, SOL</th><th>With alts</th><th>BTC, ETH, SOL</th><th>With alts</th></tr>
      ${rows}
    </table></div>
    <p>On the median day the levers barely matter: it cost ${points(Math.min(...medians), 2)} to
    ${points(Math.max(...medians), 2)} in every combination. They decide the tail.</p>
    <p>A stop on the exchange closes at the line, and walking the order book comes on top: with it, the
    worst day is at most ${points(b3.high, 1)} at 3x and ${points(b5.high, 1)} at 5x, the book taken from
    ${esc(L.book.measured)}. The book in a cascade has never been measured, so on a day like ${esc(day)} the
    fill could be worse.</p>
    <p><strong>Without a stop on the exchange</strong>, on a list with alts at 5x, the worst entry of that
    day took ${points(tail[0].worst_pct)} to ${points(tail[1].worst_pct)} of the seats' capital and liquidated
    ${Math.max(...tail.map((c) => c.seats_liquidated_on_worst))} of the five seats. How often such a day comes is
    not measured, and the venue replayed is Bybit, not Hyperliquid.</p>`;
}

/**
 * The cascade, as its own finding and never a footnote, and always next to the list of coins it was
 * measured on. The list is what decides the size of the tail: on the same day, at the same leverage
 * and under the same rules, a pool whose seats sit on the calm coins of BTC/ETH/SOL and one whose
 * seats sit on alts are two different pools. A single headline figure with no list attached is wrong
 * for one of them.
 */
function cascade(r) {
  const c = r.cascade;
  const span = (o, f) => `${f(o.low)} – ${f(o.high)}`;
  const rows = Object.values(c.cases).map((k) => `<tr>
    <td>${esc(k.label)}</td>
    <td>${span(k.loss_share_of_seats, (v) => percent(v))}</td>
    <td>${span(k.loss_usd, money)}</td>
    <td>${span(k.loss_share_of_pool, (v) => percent(v, 0))}</td>
    <td>${span(k.multiple_of_rules_cap, (v) => `${v.toFixed(1)}x`)}</td>
    <td>${span(k.loss_beyond_reserve_usd, money)}</td>
    <td>${k.years_of_base_return.low === null ? "—" : span(k.years_of_base_return, (v) => v.toFixed(1))}</td>
  </tr>`).join("");
  const worst = Math.max(...Object.values(c.cases).map((k) => k.loss_share_of_seats.high));
  return `
    ${row("Measured on", `${esc(c.day)}, a pool whose rules allow <strong>${esc(c.coins.join(", "))}</strong>`)}
    ${row("What the rules allowed that day", `${money(c.rules_cap_usd)} — ${percent(c.measured_rules.max_drawdown, 0)} of the seats' capital`)}
    ${row("What a cascade actually took", `<strong>up to ${percent(worst)} of the seats' capital</strong>`)}
    <div class="scroll"><table>
      <thead><tr><th>how the seats sit on the list</th><th>of the seats' capital</th><th>in dollars</th>
      <th>of the whole pool</th><th>times what the rules allowed</th><th>past the reserve</th>
      <th>years of the base return</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="small">Each cell is a range: the low end is a stop that executes at the next open, the high end at
    the worst price of the delay window. A price that moves that far in a minute is not bound by a line checked
    once a minute, on this venue or any other — this is a property of trading, not of this code.</p>
    <p class="notice"><strong>${esc(c.note)}</strong></p>
    ${c.rules_match ? "" : `<p class="notice">${badge("these are not the rules the cascade was measured on", "bad")}
      It was measured at ${percent(c.measured_rules.daily_loss, 0)} a day and
      ${percent(c.measured_rules.max_drawdown, 0)} from the start; yours differ, so read the table as a guide and
      not as your pool's number.</p>`}
    <p class="small muted">The 1-in-100 overshoot the rest of the page uses comes from the same measurement:
    ${span(r.worst_overshoot_range, (v) => percent(v))} of equity past the line, not the thin tail of a formula.</p>`;
}

const SHARED_POOL = `
  <p class="notice">A pool of its own starts at ${money(MIN_POOL)}. Below that the reserve cannot absorb a run of
  failures, and the fixed costs of an account eat the margin on every challenge sold.</p>
  <p>Smaller amounts belong in a <strong>shared pool</strong>: many investors hold shares in one book of seats,
  and the platform sets the layout, the rules, the price and the trader's share. That needs shares, deposits,
  withdrawals and a withdrawal queue in the contracts — <strong>the pool in this repository holds one trader and
  has none of those.</strong> It is the next step, not something running today.</p>`;

function results(byScenario) {
  const base = byScenario.base;
  const returns = SCENARIOS.map((s) => `${SCENARIO_NAME[s]} ${percent(byScenario[s].annual_return)}`).join(" · ");
  const seatRows = base.groups.map((g) => `<tr>
    <td>${money(g.F)}</td><td>${g.count}</td>
    <td>${money2(g.price)}</td>
    <td>${money2(g.min_price)}</td>
    <td>${money(g.trader_ceiling_at_take_profit)}</td>
    <td>${percent(g.busy_share, 0)}</td>
    <td>${fails(g.fails_challenge)}</td>
    <td>${fails(g.fails_funded)}</td></tr>`).join("");
  return `
    ${row("Investor's return, a year", `<strong>${esc(returns)}</strong>`)}
    ${row("Capital", `${money(base.capital)} — ${money(base.seat_capital)} in ${base.n_seats} seats,
      ${money(base.capital - base.seat_capital)} kept back`)}
    ${row("Platform's fee, a year (base)", money(base.platform_fee_year))}
    ${row("Challenges sold a year (base)", base.sold_per_year.toFixed(1))}
    ${row("Seats busy (base)", percent(base.seat_busy_share, 0))}
    ${row("Capital idle (base)", percent(base.idle_share, 0))}
    <div class="scroll"><table>
      <thead><tr><th>seat</th><th>of them</th><th>challenge price</th><th>what it costs the pool</th>
      <th>trader's ceiling</th><th>busy</th><th>failed challenges in a row the reserve takes</th>
      <th>failed funded seats in a row</th></tr></thead>
      <tbody>${seatRows}</tbody></table></div>
    <p class="small muted">The two failure columns count different things, and only the first is what the
    warning below watches: a failed challenge risks a tenth of the seat and the price paid for it offsets that,
    while a failed funded seat risks the whole seat. A pool can take a long run of the first and only a few of
    the second, which is the shape of the design, not a fault in it.</p>
    <p class="small muted">"What it costs the pool" is the minimum price: at or below it a sold challenge loses
    money for the investor. "The trader's ceiling" is what one trader takes if a funded seat runs all the way to
    the take-profit of the base scenario. All of it is the model's, at the three scenarios named above: they
    differ in what nobody has measured — how many buyers a month, how far a stop overshoots, how strong the
    traders are.</p>
    ${warnings(base)}`;
}

const fails = (n) => (n === null ? "no limit" : n);

function warnings(r) {
  const said = {
    price_below_cost: badge("the price is below what a challenge costs the pool — the pool loses money on every sale", "bad"),
    reserve_takes_few_failures: badge("the reserve takes fewer than ten failures in a row on the largest seat", "bad"),
    reserve_short_of_a_shock: badge("if every funded seat is stopped at once, the reserve does not cover the overshoot", "bad"),
    cascade_beyond_rules: badge("a measured cascade takes more than the drawdown rule allows", "bad"),
    cascade_beyond_reserve: badge("a measured cascade takes more than the reserve holds", "bad"),
    cascade_measured_on_other_rules: badge("the cascade was measured at other rules than these", "bad"),
  };
  if (!r.warnings.length) return `<p>${badge("no warnings at these settings", "ok")}</p>`;
  return `<p>${r.warnings.map((w) => said[w] || badge(w, "bad")).join(" ")}</p>`;
}
