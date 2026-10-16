// A pool of many traders, for its traders (#/multi, #/multi/<pool>, #/exam/<exam>): the pools a deployment lists,
// one pool's menu and plan, and one paper exam with the calls that move it. A draft: the contracts are drafts, and
// until a deployment's record is in CONFIG.multi these pages list nothing. What is read and what may be sent is in
// app/lib/multi.js.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { CONFIG } from "../config.js";
import { esc, render, $, wire, badge, row } from "../lib/ui.js";
import { stopInputs } from "./trading.js";
import { EXAM_STATE, EXAM_STATUS, SEAT_STATE, BREACH, planView, boundsView, examActions, seatActions, parseInvite,
  sizeUnits, coinAmount, myExams, rememberExam, usdc6, span } from "../lib/multi.js";

const DRAFT = "A draft: the contracts behind these pages are not on main and not reviewed by an audit.";
const deployments = () => CONFIG.multi || [];
const deploymentOf = (pool) => deployments().find((d) => d.pool.toLowerCase() === String(pool).toLowerCase()) || null;
const store = () => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};
const spot = (x) => Number(x) / 1e8;   // value() is counted on HyperCore's spot balance, 1e8 = 1 USDC
const when = (t) => (Number(t) ? `${new Date(Number(t) * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC` : "—");
const refresh = () => window.dispatchEvent(new HashChangeEvent("hashchange"));

/** The buttons of a list of actions: open ones pressable, each with who may press it and why it is or is not open. */
function buttons(list, prefix) {
  return list.map((a) => `<p class="small"><button class="secondary small" id="${prefix}-${a.id}"${a.enabled ? "" : " disabled"}>
    ${esc(a.label)}</button> <span class="muted">${esc(a.who)}: ${esc(a.why)}</span></p>`).join("");
}

function notListed(page, what) {
  render(page, `<section class="card"><h2>Not ${esc(what)} this site lists</h2>
    <p>Pools of many traders are listed from their deployment's record, and none is listed under this address.</p>
    <p><a href="#/multi">The pools of many traders</a></p></section>`);
}

export async function multiListView(page) {
  const list = deployments();
  if (!list.length) {
    render(page, `<section class="card"><h2>Pools of many traders</h2>
      <p><strong>${esc(DRAFT)}</strong></p>
      <p>None is deployed yet. Once a deployment of these contracts is made, its pool is listed here from its record:
      its menu of sizes, the seats still free, its term and its rules, and the exams a trader can take on it.</p></section>`);
    return;
  }
  render(page, `<section><h2>Pools of many traders</h2><p><strong>${esc(DRAFT)}</strong></p>
    <div id="multi-pools" class="grid"></div></section>`);
  const cards = await Promise.all(list.map(async (d) => {
    const pool = chain.contract("multi", d.pool);
    const [plan, owner, value, endsAt, closing, productBounds] = await Promise.all([
      pool.plan(), pool.owner(), pool.value(), pool.endsAt(), pool.closing(), pool.productBounds()]);
    const rooms = await Promise.all(plan.menu.map((_, line) => pool.room(line)));
    const v = planView(plan);
    return `<article class="card"><h3><a href="#/multi/${esc(d.pool)}">${esc(chain.short(d.pool))}</a>
        ${productBounds ? "" : badge("Test bounds", "warn")} ${closing ? badge("Not selling", "") : ""}</h3>
      ${row("Owner", esc(chain.short(owner)))}
      ${row("Value", `${spot(value).toFixed(2)} USDC`)}
      ${row("Ends", esc(when(endsAt)))}
      ${v.menu.map((m) => row(`Exam of ${m.size} USDC`, `${m.price} USDC · ${Number(rooms[m.line])} left`)).join("")}
      <p><a class="button" href="#/multi/${esc(d.pool)}">See the pool</a></p></article>`;
  }));
  $("#multi-pools", page).innerHTML = cards.join("");
}

export async function multiPoolView(address, page) {
  const d = deploymentOf(address);
  if (!d) return notListed(page, "a pool of many traders");
  const pool = chain.contract("multi", d.pool);
  const factory = chain.contract("factory", d.factory);
  const [plan, owner, value, inWork, held, endsAt, closing, saleLead, productBounds, minTerm, maxTerm, notice, seats,
    freeMonth, freeTaken, freeLength, armWait, startedAt, fee, now] = await Promise.all([
    pool.plan(), pool.owner(), pool.value(), pool.inWork(), pool.held(), pool.endsAt(), pool.closing(), pool.saleLead(),
    pool.productBounds(), pool.minTerm(), pool.maxTerm(), pool.notice(), pool.seats(), pool.freeMonth(), pool.freeTaken(),
    pool.FREE_MONTH(), pool.ARM_WAIT(), pool.startedAt(), factory.challengeFee(), chain.blockTime()]);
  const v = planView(plan);
  const bounds = boundsView({ productBounds, minTerm, maxTerm, notice });
  const rooms = await Promise.all(v.menu.map((m) => pool.room(m.line)));
  const infos = await Promise.all(seats.map((s) => pool.seatInfo(s)));
  const me = chain.currentAddress();
  const month = Math.floor((now - Number(startedAt)) / Number(freeLength));
  const freeLeft = v.freePerMonth - (Number(freeMonth) === month ? Number(freeTaken) : 0);
  const mine = me ? myExams(store(), d.pool, me) : [];
  const ended = now >= Number(endsAt);
  const assetName = await names();

  render(page, `
    <section class="card">
      <h2>Pool of many traders ${esc(chain.short(d.pool))} ${bounds.test ? badge("Test bounds", "warn") : ""}</h2>
      <p><strong>${esc(DRAFT)}</strong></p>
      <p class="small">${esc(bounds.words)}</p>
      ${row("Owner", esc(owner))}
      ${row("Value", `${spot(value).toFixed(2)} USDC`)}
      ${row("Spoken for", `${usdc6(inWork)} USDC in ${Number(held)} of ${v.seats} seats; at most ${esc(v.inWork)} of the value`)}
      ${row("Runs until", esc(when(endsAt)))}
      ${row("Selling", closing ? "no: the last stretch of the term" : `yes, until ${esc(span(saleLead))} before the end`)}
    </section>
    <section class="card"><h3>The menu</h3>
      <p class="small muted">An exam is traded on nobody's money. Its size is spoken for from the purchase; a trader who passes
      takes a funded account of that size on the pool's capital. Each purchase pays the price to the pool and
      ${chain.usd6(fee)} USDC to the platform.</p>
      ${v.menu.map((m) => `<p>${row(`Exam of ${m.size} USDC`, `${m.price} USDC · ${Number(rooms[m.line])} left`)}
        <button id="buy-${m.line}"${closing || Number(rooms[m.line]) === 0 ? " disabled" : ""}>Buy this exam</button></p>`).join("")}
    </section>
    ${v.freePerMonth ? `<section class="card"><h3>A free exam, on an invitation</h3>
      <p class="small muted">The creator gives ${v.freePerMonth} a month, each on an invitation signed for one wallet.
      ${Math.max(freeLeft, 0)} left this month.</p>
      <p><textarea id="invite" rows="3" placeholder='{"pool": "0x…", "trader": "0x…", "line": 0, …}'></textarea></p>
      <p><button id="take-free">Take the free exam</button></p></section>` : ""}
    <section class="card"><h3>Your exams</h3>
      <div id="mine">${mine.length ? mine.map((x) => `<p><a href="#/exam/${esc(x)}">${esc(x)}</a></p>`).join("")
        : `<p class="muted">${me ? "None bought from this browser." : "Connect a wallet to see the exams bought from this browser."}</p>`}</div>
      <p class="small"><input id="exam-addr" placeholder="0x… an exam's address"> <button class="secondary small" id="exam-go">Open</button></p>
    </section>
    <section class="card"><h3>The plan</h3>
      ${row("Exam targets", esc(v.exam.targets.join(", then ")))}
      ${row("Exam rules", esc(`${v.examRules.drawdown} drawdown, ${v.examRules.dailyLoss} a day, ${v.examRules.leverage} leverage`))}
      ${row("Exam assets", esc(v.examRules.assets.map((a) => assetName(a)).join(", ")))}
      ${row("A step may take", esc(v.exam.step))}
      ${row("A step may go without a fill", esc(v.exam.idle))}
      ${row("Trading days a step needs", String(v.exam.tradingDays))}
      ${row("Cost of a fill", esc(v.exam.cost))}
      ${row("A fill", esc(`from ${v.exam.fillDelay} blocks after the order, for ${v.exam.fillWindow} blocks`))}
      ${row("Funded rules", esc(`${v.fundedRules.drawdown} drawdown, ${v.fundedRules.dailyLoss} a day, ${v.fundedRules.leverage} leverage`))}
      ${row("Funded target", esc(v.fundedTarget))}
      ${row("At the target", esc(v.afterTarget))}
      ${row("Trader's share of the profit", esc(v.traderShare))}
      ${row("Time to take the account after a pass", esc(v.claimWindow))}
      ${row("A funded account may go without a trade", esc(v.fundedIdle))}
      ${row("Term", esc(v.term))}
    </section>
    <section class="card"><h3>Seats</h3>
      ${seats.length ? seats.map((s, i) => `<div class="seat"><h4>${esc(chain.short(s))} ${badge(SEAT_STATE[Number(infos[i].state)] || "?", "")}</h4>
        ${Number(infos[i].state) >= 2 ? row("Trader", esc(infos[i].trader)) : ""}
        ${buttons(seatActions(infos[i], { me, now, armWait, afterTarget: plan.afterTarget, ended, stage: null }), `seat${i}`)}</div>`).join("")
        : "<p class=\"muted\">No seat yet.</p>"}
      <p class="small muted">Anyone may press these: no keeper calls them yet. The contract refuses what is not due.</p>
    </section>`);

  v.menu.forEach((m) => wire($(`#buy-${m.line}`, page), async () => {
    const who = chain.currentAddress() || (await chain.connect());
    await chain.approveIfNeeded(d.pool, BigInt(plan.menu[m.line].price) + BigInt(fee));
    const exam = soldExam(await chain.write("multi", d.pool, "buy", [m.line]));
    rememberExam(store(), d.pool, who, exam);
    location.hash = `#/exam/${exam}`;
    return "Bought.";
  }));
  wire($("#take-free", page), async () => {
    const who = chain.currentAddress() || (await chain.connect());
    const got = parseInvite($("#invite", page).value, { pool: d.pool, me: who, now: await chain.blockTime(), lines: v.menu.length });
    if (got.error) throw new Error(got.error);
    const i = got.invite;
    const exam = soldExam(await chain.write("multi", d.pool, "takeFree", [i.line, i.nonce, i.expiresAt, i.v, i.r, i.s]));
    rememberExam(store(), d.pool, who, exam);
    location.hash = `#/exam/${exam}`;
    return "Taken.";
  });
  $("#exam-go", page)?.addEventListener("click", () => {
    const a = $("#exam-addr", page).value.trim();
    if (/^0x[0-9a-fA-F]{40}$/.test(a)) location.hash = `#/exam/${a}`;
  });
  seats.forEach((s, i) => wireSeat(page, `seat${i}`, d.pool, s));
}

/** The exam a purchase made, from the pool's own event in its receipt. */
function soldExam(receipt) {
  const iface = chain.contract("multi", receipt.to).interface;
  for (const log of receipt.logs) {
    try {
      const parsed = iface.parseLog(log);
      if (parsed && parsed.name === "ExamSold") return parsed.args.exam;
    } catch {
      // another contract's event
    }
  }
  throw new Error("The purchase went through, but its receipt names no exam.");
}

function wireSeat(page, prefix, pool, seat) {
  const plain = { open: [seat], rearm: [seat], reopen: [seat], finish: [seat], sweep: [seat], noteActivity: [seat] };
  for (const id of Object.keys(plain)) {
    wire($(`#${prefix}-${id}`, page), async () => {
      await chain.write("multi", pool, id, plain[id]);
      refresh();
    });
  }
  // These end a funded stage: the orders resting on its account are named for the contract to cancel, and
  // what it holds outside the plan's assets, as a breach is (app/views/trading.js).
  for (const id of ["hitTarget", "closeIdle", "endSeat"]) {
    wire($(`#${prefix}-${id}`, page), async () => {
      const rules = await chain.contract("pool", seat).rules();
      const { cancels, extra } = await stopInputs(seat, rules);
      await chain.write("multi", pool, id, [seat, cancels, extra, chain.randomSalt()]);
      refresh();
    });
  }
}

/** Asset indices to names, from Hyperliquid's list of perps; a number where the list does not answer. */
async function names() {
  const perps = await hl.perps().catch(() => []);
  return (i) => perps.find((p) => Number(p.index) === Number(i))?.name || `#${i}`;
}

export async function examView(address, page) {
  const exam = chain.contract("exam", address);
  const poolAddress = await exam.pool().catch(() => null);
  const d = poolAddress && deploymentOf(poolAddress);
  if (!d) return notListed(page, "an exam of a pool");
  const pool = chain.contract("multi", d.pool);
  const [trader, agent, status, step, deadline, lastFillAt, passedAt, endReason, endEquity, day, tradingDays, rules, terms,
    pending, info, plan, block, now] = await Promise.all([
    exam.trader(), exam.agent(), exam.status(), exam.step(), exam.deadline(), exam.lastFillAt(), exam.passedAt(),
    exam.endReason(), exam.endEquity(), exam.day(), exam.tradingDays(), exam.rules(), exam.terms(), exam.pending(),
    pool.examInfo(address), pool.plan(), chain.readProvider.getBlockNumber(), chain.blockTime()]);
  const active = Number(status) === 1;
  // Equity, notional and the verdict read each held asset's mark; a mark that does not answer leaves them unread.
  const [equity, notional, violation] = active
    ? await Promise.all([exam.equity(), exam.notional(), exam.violation()]).catch(() => [null, null, null])
    : [endEquity, 0n, 0];
  const assets = [...rules.assets].map(Number);
  const positions = await Promise.all(assets.map((a) => exam.positionOf(a)));
  const perps = await hl.perps().catch(() => []);
  const perp = (i) => perps.find((p) => Number(p.index) === i) || { name: `#${i}`, szDecimals: 0 };
  const me = chain.currentAddress();
  const e = { status, step, deadline, maxIdle: terms.maxIdle, lastFillAt, trader, agent, pending, fillDelay: terms.fillDelay,
    fillWindow: terms.fillWindow, capital: terms.capital, targetsBps: [...terms.targetsBps], equity: equity ?? 0n,
    notional: notional ?? 1n, violation: violation ?? 0, tradingDays, minTradingDays: terms.minTradingDays, day, passedAt,
    claimWindow: plan.claimWindow, poolState: info.state };
  const actions = examActions(e, me, now, block);
  const target = (BigInt(terms.capital) * (10_000n + BigInt(terms.targetsBps[Number(step)] ?? 0))) / 10_000n;
  const waiting = BigInt(pending.sz) !== 0n;

  render(page, `
    <section class="card">
      <h2>Paper exam ${esc(chain.short(address))} ${badge(EXAM_STATUS[Number(status)] || "?", Number(status) === 2 ? "ok" : Number(status) >= 3 ? "bad" : "")}</h2>
      <p><strong>${esc(DRAFT)}</strong></p>
      <p class="small muted">Traded on nobody's money: the orders are transactions, the position is a number in the contract,
      and every price is the mark the chain shows in the block that fills it.</p>
      ${row("Pool", `<a href="#/multi/${esc(d.pool)}">${esc(chain.short(d.pool))}</a> · ${esc(EXAM_STATE[Number(info.state)] || "?")}`)}
      ${row("Trader", esc(trader))}
      ${row("Size", `${usdc6(terms.capital)} USDC`)}
      ${row("Step", `${Number(step) + 1} of ${terms.targetsBps.length}: target ${usdc6(target)} USDC`)}
      ${row(active ? "Equity" : "Equity at the end", equity === null ? "a mark did not answer" : Number(status) === 4 ? "not recorded for an expired exam" : `${usdc6(equity)} USDC`)}
      ${row("Open notional", notional === null ? "—" : `${usdc6(notional)} USDC`)}
      ${row("Rules", `${esc(BREACH[Number(violation ?? 0)] === "None" ? "none broken now" : `${BREACH[Number(violation)]} broken now`)}`)}
      ${row("Deadline", esc(when(deadline)))}
      ${row("Trading days", `${Number(tradingDays)} of ${Number(terms.minTradingDays)}`)}
      ${Number(status) >= 3 ? row("Ended", esc(Number(status) === 3 ? `breached: ${BREACH[Number(endReason)]}` : "expired")) : ""}
      ${Number(status) === 2 ? row("Passed", esc(when(passedAt))) : ""}
    </section>
    <section class="card"><h3>Positions</h3>
      ${assets.map((a, i) => row(perp(a).name, esc(coinAmount(positions[i], perp(a).szDecimals)))).join("")}
      <h4>Waiting order</h4>
      ${waiting ? `<p>${esc(pending.isBuy ? "Buy" : "Sell")} ${esc(coinAmount(pending.sz, perp(Number(pending.asset)).szDecimals))}
        ${esc(perp(Number(pending.asset)).name)}, placed at block ${Number(pending.placedBlock)}; this is block ${block}.</p>`
        : "<p class=\"muted\">None.</p>"}
      <form id="order-form">
        <label>Asset <select name="asset">${assets.map((a) => `<option value="${a}">${esc(perp(a).name)}</option>`).join("")}</select></label>
        <label>Side <select name="side"><option value="buy">Buy</option><option value="sell">Sell</option></select></label>
        <label>Size <input name="size" placeholder="0.005"></label>
      </form>
    </section>
    <section class="card"><h3>What can be done now</h3>
      ${buttons(actions, "exam")}
      <p class="small muted">"Anyone" means any wallet: no keeper fills orders or takes the day's snapshot yet.
      The contract refuses what is not due, and its refusal shows here.</p>
    </section>`);

  wire($("#exam-place", page), async () => {
    const f = new FormData($("#order-form", page));
    const asset = Number(f.get("asset"));
    const sz = sizeUnits(f.get("size"), perp(asset).szDecimals);
    await chain.write("exam", address, "place", [asset, f.get("side") === "buy", sz]);
    refresh();
  });
  for (const id of ["fill", "checkpoint", "breach", "expire", "graduate"]) {
    wire($(`#exam-${id}`, page), async () => {
      await chain.write("exam", address, id, []);
      refresh();
    });
  }
  wire($("#exam-claim", page), async () => {
    const receipt = await chain.write("multi", d.pool, "claim", [address]);
    const claimed = receipt.logs.map((l) => {
      try {
        return pool.interface.parseLog(l);
      } catch {
        return null;
      }
    }).find((p) => p && p.name === "Claimed");
    location.hash = `#/multi/${d.pool}`;
    return claimed ? `The account is seat ${chain.short(claimed.args.seat)}: open it on the pool's page once the money is there.` : "Taken.";
  });
  wire($("#exam-release", page), async () => {
    await chain.write("multi", d.pool, "release", [address]);
    refresh();
  });
}
