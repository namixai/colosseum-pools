// The trading page of an exam (#/exam/<exam address>): a chart and a book of Hyperliquid's open data, an order
// form, the waiting order, the positions with their stops and takes, the account against its lines, and the journal.
// An order here is a transaction to the trader's own exam contract (PaperExam.place), not an order on any exchange.
// Every sum is app/lib/exam.js's; what is said and where a number sits is app/lib/examscreen.js; this file reads, draws
// and sends. It is not app/views/trading.js, which is another product's page.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { CONFIG } from "../config.js";
import { esc, render, $, isAddress, friendly } from "../lib/ui.js";
import { readLogs } from "../lib/simpool.js";
import {
  PAPER_EXAM_ABI, dataSource, examAccount, oraclePxCall, bboCall, bboOf, placeCall, graduateCall, setAgentCall,
  setTriggerCall, clearTriggerCall, refusalOf, candleRows,
} from "../lib/exam.js";
import {
  SCREEN_TEXT, WHAT_THIS_IS, ESTIMATE_NOTE, INTERVALS, BOOK_DEPTH, candleWindow, readsFrom, accountFigures, positionRow,
  pendingText, accountScale, orderForm, previewView, triggerForm, triggerPreviewView, bookView, chartScale, candleText,
  journalRows, sendFailure, timeLeft, networkMarks, lifeSteps, timeText, markUnknown, isKnown, singleFlight, journalPlan, journalRange,
} from "../lib/examscreen.js";
import { pxText } from "../lib/exam.js";

const { ethers } = window;
const POLL_MS = 5000;
const BOOK_MS = 4000;
const CANDLES_MS = 20000;
const JOURNAL_WINDOW = 50;
const JOURNAL_CALLS = 20;
const ZERO = "0x0000000000000000000000000000000000000000";
const keccak = (hex) => ethers.keccak256(hex);
const same = (a, b) => Boolean(a && b && String(a).toLowerCase() === String(b).toLowerCase());
const keyName = (exam) => `exam-second-key-${String(exam).toLowerCase()}`;

function store(name, value) {
  try {
    if (value === undefined) return localStorage.getItem(name);
    if (value === null) localStorage.removeItem(name);
    else localStorage.setItem(name, value);
  } catch {
    // storage is blocked: a second key is then kept for this tab only (see `kept`)
  }
  return null;
}
const kept = new Map();  // the second key where storage is blocked, for this tab

export async function examView(address, page) {
  if (!isAddress(address)) {
    render(page, `<section class="card narrow"><h2>Exam</h2>
      <p>${esc(WHAT_THIS_IS)}</p>
      <p>Open the page from the exam's address:</p>
      <div class="inline"><input id="addr" placeholder="0x…" size="44"><button id="go">Open</button></div></section>`);
    $("#go", page).addEventListener("click", () => { location.hash = `#/exam/${$("#addr", page).value.trim()}`; });
    return;
  }
  if ((await chain.readProvider.getCode(address)) === "0x") {
    render(page, `<section class="card narrow"><h2>Exam</h2><p>There is no contract at this address on the network this page reads.</p>
      <p><a href="#/exam">Back</a></p></section>`);
    return;
  }
  const exam = new ethers.Contract(address, PAPER_EXAM_ABI, chain.readProvider);
  // The chain the exam is on is the one the node that answers its getters says (eth_chainId): the page's data source
  // is that chain's or nothing (dataSource), never another network's candles beside this exam.
  const chainId = await nodeChainId();
  const ds = dataSource({ chainId }, CONFIG);
  const [rules, terms, trader] = await Promise.all([exam.rules(), exam.terms(), exam.trader()]);
  const assets = [...rules.assets].map(Number);
  const perps = new Map();
  if (ds.ok) for (const a of assets) perps.set(a, markUnknown(await hl.perp(a), a));
  const st = {
    exam, address, ds, perps, assets, trader, asset: assets[0], isBuy: true, interval: "15m", snap: null, rows: [],
    book: null, hover: null, journal: [], timers: [],
    times: new Map(), tab: "pos", triggers: {}, flight: singleFlight(), loaded: null,
  };

  page.className = "ex-page";
  render(page, skeleton(st));
  wire(page, st);
  // The first reading decides whether the page can be drawn: a failure here is the page's failure, not a panel's.
  await tick(page, st);
  paintJournal(page, st);
  if (ds.ok) {
    paintAssets(page, st);
    refreshCandles(page, st);
    refreshBook(page, st);
    st.timers.push(setInterval(() => refreshBook(page, st), BOOK_MS), setInterval(() => refreshCandles(page, st), CANDLES_MS));
  }
  st.timers.push(setInterval(() => tick(page, st).catch((err) => note(page, `Could not refresh: ${friendly(err)}`)), POLL_MS));
  // A page that was left stops its readings: it is no longer in the document.
  st.timers.push(setInterval(() => { if (!page.isConnected) st.timers.forEach(clearInterval); }, 1000));
}

/** The chain id the node itself answers. Asked of the node directly: the read provider is built with the app's own
 *  chain (staticNetwork) and answers that one without asking, which would make the check a mirror. */
async function nodeChainId() {
  const res = await fetch(CONFIG.rpc, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_chainId", params: [] }) });
  if (!res.ok) throw Object.assign(new Error(`The node's chain id: HTTP ${res.status}`), { status: res.status });
  const answer = await res.json();
  const id = Number(answer?.result);
  if (!Number.isInteger(id)) throw new Error("The node gave no chain id.");
  return id;
}

function note(page, text) {
  const box = $("#ex-error", page);
  if (box) {
    box.textContent = text;
    box.hidden = !text;
  }
}

// ── the skeleton, drawn once; the panels are filled by the readings ──

function skeleton(st) {
  const { ds } = st;
  const marks = networkMarks(CONFIG);
  const intervals = INTERVALS.map((i) => `<button type="button" data-interval="${i.key}"${i.key === st.interval ? ' class="on"' : ""}>${i.key}</button>`).join("");
  const options = st.assets.map((a) => `<option value="${a}">${esc(st.perps.get(a)?.name ?? `#${a}`)}</option>`).join("");
  const trigger = (k, label) => `<div class="ex-tr"><label>Set ${label}<input id="ex-${k}-px" inputmode="decimal" autocomplete="off" placeholder="price"></label>
      <label>Size<input id="ex-${k}-sz" inputmode="decimal" autocomplete="off" placeholder="all"></label>
      <button type="button" class="ex-mini2" id="ex-${k}-set" data-set="${k}" disabled>Set ${label}</button></div>
      <div class="ex-prev" id="ex-${k}-prev"></div>`;
  return `
    <div class="ex-strip">
      <span class="ex-badge">${esc(SCREEN_TEXT.strip)}</span>${marks ? `<span class="ex-tag">${esc(marks.tag)}</span>` : ""}
      <p class="ex-line">${esc(WHAT_THIS_IS)}</p>
      <div class="ex-kv" id="ex-kv"></div>
      <div class="ex-steps" id="ex-steps"></div>
    </div>
    <p class="ex-notice" id="ex-error" role="status" hidden></p>
    ${ds.ok ? "" : `<section class="ex-pane ex-pad"><h3>Chart and book not shown</h3><p>${esc(ds.text)}</p></section>`}
    <div class="ex-phtabs"${ds.ok ? "" : " hidden"}><button type="button" class="on" data-phone="chart">Chart</button><button type="button" data-phone="book">Book</button></div>
    <div class="ex-term" id="ex-term" data-phone="chart">
      <section class="ex-pane ex-chart"${ds.ok ? "" : " hidden"}>
        <div class="ex-mkt">
          <label class="ex-sel"><select id="ex-asset" aria-label="Asset">${options}</select></label>
          <div class="ex-px"><b id="ex-px">—</b><span>Oracle price</span></div>
          <div class="ex-iv" role="group" aria-label="Interval">${intervals}</div>
          <span class="ex-cap">${esc(SCREEN_TEXT.chart)}</span>
        </div>
        <div class="ex-cw"><canvas id="ex-canvas" role="img" aria-label="Candle chart"></canvas></div>
        <p class="ex-hover" id="ex-hover">&nbsp;</p>
      </section>
      <section class="ex-pane ex-book"${ds.ok ? "" : " hidden"}>
        <div class="ex-ph"><b>Book</b><span class="ex-cap">${esc(SCREEN_TEXT.book)}</span></div>
        <div id="ex-book" class="ex-muted ex-pad">Loading…</div>
        <p class="ex-bn">${esc(SCREEN_TEXT.bookFill)}</p>
      </section>
      <div class="ex-right">
        <section class="ex-pane ex-form">
          <div class="ex-tabs" id="ex-assets" role="tablist" aria-label="Asset"></div>
          <div class="ex-bs" role="group" aria-label="Side">
            <button type="button" data-side="buy" class="on b">Buy / Long</button>
            <button type="button" data-side="sell" class="s">Sell / Short</button>
          </div>
          <label class="ex-fl">Size<span class="ex-inp"><input id="ex-size" inputmode="decimal" autocomplete="off" placeholder="0.00"><span id="ex-unit"></span></span></label>
          <div id="ex-preview" class="ex-est"></div>
          <button type="button" id="ex-send" class="ex-go b" disabled>Buy / Long</button>
          <p class="ex-sign" id="ex-signs"></p>
          <p class="ex-result" id="ex-send-out"></p>
          <div class="ex-trg">${trigger("stop", "stop")}${trigger("take", "take")}</div>
        </section>
        <div class="ex-pane ex-pend" id="ex-pending" hidden></div>
        <section class="ex-pane ex-acct">
          <div class="ex-ph"><b>Exam account</b></div>
          <div id="ex-account"></div>
        </section>
        <section class="ex-pane ex-pass">
          <button type="button" id="ex-graduate" class="ex-go" disabled>Pass the step</button>
          <p class="ex-why" id="ex-graduate-text"></p>
          <p class="ex-result" id="ex-graduate-out"></p>
        </section>
        <section class="ex-pane ex-key">
          <div class="ex-ph"><b>Trade without wallet prompts</b><span class="ex-tg" id="ex-tg" aria-hidden="true"><i></i></span></div>
          <p>${esc(SCREEN_TEXT.secondKey)}</p>
          <p>${esc(SCREEN_TEXT.gas)}</p>
          <div id="ex-agent" class="ex-agent"></div>
        </section>
      </div>
      <section class="ex-pane ex-bottom">
        <div class="ex-tabs" role="tablist"><button type="button" data-tab="pos" class="on" id="ex-tab-pos">Positions</button><button type="button" data-tab="jrnl" id="ex-tab-jrnl">Journal</button></div>
        <div id="ex-positions" class="ex-scroll"></div>
        <div id="ex-journal-pane" hidden>
          <div id="ex-journal" class="ex-scroll"></div>
          <p class="ex-pad"><button type="button" class="ex-mini2" id="ex-journal-load">Load recent events</button> <button type="button" class="ex-mini2" id="ex-journal-new" hidden>Load newer events</button> <span class="ex-muted" id="ex-journal-note"></span></p>
        </div>
      </section>
    </div>
    ${marks ? `<div class="ex-foot"><span class="ex-disc">${esc(marks.plaque)}</span></div>` : ""}`;
}

// ── readings ────────────────────────────────────────────────────

const attempt = (fn) => Promise.resolve().then(fn).catch(() => null);

/** Everything the account needs, read from the chain at its latest block. A price that could not be read is null. */
async function readExam(st) {
  const { exam } = st;
  const [block, got] = await Promise.all([
    chain.readProvider.getBlock("latest"),
    Promise.all([exam.status(), exam.step(), exam.cash(), exam.dayStartEquity(), exam.tradingDays(), exam.deadline(), exam.lastFillAt(),
      exam.pending(), exam.filler(), exam.fromBook(), exam.maxWorseBps(), exam.drawdownKind(), exam.peakSnapshot(), exam.agent(),
      exam.rules(), exam.terms()]),
  ]);
  const [status, step, cash, dayStartEquity, tradingDays, deadline, lastFillAt, pending, filler, fromBook, maxWorseBps,
    drawdownKind, peakSnapshot, agent, rules, terms] = got;
  const perAsset = await Promise.all(st.assets.map(async (asset) => {
    const [size, stop, take, oracleHex, bboHex] = await Promise.all([
      exam.positionOf(asset), attempt(() => exam.stopOf(asset)), attempt(() => exam.takeOf(asset)),
      attempt(() => chain.readProvider.call(oraclePxCall(asset))), attempt(() => chain.readProvider.call(bboCall(asset))),
    ]);
    const oracle = oracleHex && oracleHex !== "0x" ? BigInt(oracleHex) : null;
    const bbo = bboOf(bboHex);
    return { asset, size, stop, take, oracle: oracle === 0n ? null : oracle, bid: bbo?.bid ?? null, ask: bbo?.ask ?? null };
  }));
  const reads = readsFrom({ status, step, cash, dayStartEquity, tradingDays, deadline, lastFillAt, pending, filler, fromBook,
    maxWorseBps, drawdownKind, peakSnapshot, rules, terms }, perAsset);
  const now = block.timestamp;
  const account = examAccount(reads, { now, block: block.number });
  return { reads, account, perAsset, agent, block: block.number, now };
}

async function tick(page, st) {
  st.snap = await readExam(st);
  note(page, "");
  paintAccount(page, st);
  paintPositions(page, st);
  paintTicket(page, st);
  paintAgent(page, st);
}

function refreshCandles(page, st) {
  const perp = st.perps.get(st.asset);
  const { startTime, endTime } = candleWindow(st.interval, Date.now());
  const wanted = `${st.asset}/${st.interval}`;
  hl.candles(perp.name, st.interval, startTime, endTime).then((raw) => {
    if (`${st.asset}/${st.interval}` !== wanted) return;
    st.rows = candleRows(raw);
    drawChart(page, st);
  }).catch((err) => {
    $("#ex-hover", page).textContent = `Chart: could not load (${friendly(err)})`;
  });
}

function refreshBook(page, st) {
  const perp = st.perps.get(st.asset);
  const wanted = st.asset;
  hl.book(perp.name).then((raw) => {
    if (wanted !== st.asset) return;
    st.book = bookView(raw, BOOK_DEPTH);
    paintBook(page, st);
  }).catch((err) => {
    $("#ex-book", page).textContent = `Could not load: ${friendly(err)}`;
  });
}

// ── painting ────────────────────────────────────────────────────

function paintAssets(page, st) {
  $("#ex-assets", page).innerHTML = st.assets.map((a) =>
    `<button type="button" role="tab" data-asset="${a}"${a === st.asset ? ' class="on"' : ""} aria-selected="${a === st.asset}">${esc(st.perps.get(a)?.name ?? `#${a}`)}</button>`).join("");
  $("#ex-unit", page).textContent = st.perps.get(st.asset)?.name ?? "";
  const sel = $("#ex-asset", page);
  if (sel) sel.value = String(st.asset);
}

function paintHead(page, st) {
  const perp = st.perps.get(st.asset);
  const oracle = st.snap?.perAsset.find((a) => a.asset === st.asset)?.oracle;
  const px = $("#ex-px", page);
  if (px) px.textContent = perp && oracle ? pxText(oracle, perp.szDecimals) : "—";
}

function paintAccount(page, st) {
  const { account, now } = st.snap;
  const f = accountFigures(account);
  $("#ex-kv", page).innerHTML = [["Size", f.size], ["Step", f.step], ["Status", f.status]]
    .map(([k, v]) => `<span><i>${esc(k)}</i><b>${esc(v)}</b></span>`).join("");
  $("#ex-steps", page).innerHTML = lifeSteps(account).map((x) => `<span class="${x.state}">${x.n} ${esc(x.label)}</span>`).join("");
  const scale = accountScale(account);
  const lines = [...f.lines].sort((x, y) => Number(y.near) - Number(x.near));
  const idx = new Map(account.lines.map((l, i) => [l.kind, i]));
  $("#ex-account", page).innerHTML = `
    <dl class="ex-eq"><div><dt>Equity</dt><dd>${esc(f.equity)}</dd></div><div><dt>Balance</dt><dd>${esc(f.balance)}</dd></div>
      <div><dt>Leverage</dt><dd>${esc(f.leverage)} <small>of ${esc(f.maxLeverage)}</small></dd></div></dl>
    <div class="ex-scale">
      ${f.target ? `<div class="ex-sl"><span>Target</span><b>${esc(f.target.at)}</b><em>to go ${esc(f.target.toGo)}</em></div>` : ""}
      ${scale ? `<div class="ex-bar${scale.broken ? " broken" : ""}" role="img" aria-label="Equity between the loss limits and the target">
        ${scale.lines.map((l) => `<i class="${l.near ? "near" : ""}" style="left:${l.share / 100}%"></i>`).join("")}
        <i class="tl" style="left:${scale.target / 100}%"></i><i class="now" style="left:${scale.equity / 100}%"></i></div>` : ""}
      ${lines.map((l) => `<div class="ex-sl${l.near ? " near" : ""}"><span>${esc(l.label)}</span><b>${esc(l.at)}</b><em>room ${esc(l.room)}${l.from ? ` · ${esc(l.from)}` : ""}</em></div>`).join("")}
      <div class="ex-sl"><span>Time left</span><b>${esc(account.time.endsAt === null ? "—" : timeLeft(account.time.endsAt, now))}</b>${f.days ? `<em>trading days ${esc(f.days)}</em>` : ""}</div>
    </div>`;
  void idx;
  const g = $("#ex-graduate", page);
  g.disabled = !account.graduate.can || st.flight.busy;
  $("#ex-graduate-text", page).textContent = account.graduate.can ? "" : account.graduate.text;
}

function paintPositions(page, st) {
  const { account } = st.snap;
  const box = $("#ex-positions", page);
  if (!st.ds.ok) {
    box.innerHTML = `<p class="ex-pad ex-muted">Positions are not shown: the asset list of this network was not read.</p>`;
    return;
  }
  const rows = account.positions.map((p) => positionRow(p, st.perps));
  const pending = pendingText(account.pending, st.perps);
  const pend = $("#ex-pending", page);
  pend.hidden = !pending;
  pend.innerHTML = pending ? `<span class="ex-dot"></span>${esc(pending.text)}${account.pending.state === "lapsed" ? "" : `<p class="ex-why">${esc(SCREEN_TEXT.oneAtATime)}</p>`}` : "";
  $("#ex-tab-pos", page).textContent = `Positions (${rows.length})`;
  box.innerHTML = (rows.length ? `<table class="ex-t ex-cards ex-pos"><thead><tr><th>Asset</th><th>Side</th><th>Size</th><th>Mark</th><th>Notional</th><th>Leverage</th><th>Stop / take</th><th></th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td data-l="Asset"><b>${esc(r.name)}</b></td><td data-l="Side" class="${r.side === "Long" ? "lg" : "sh"}">${esc(r.side)}</td><td data-l="Size">${esc(r.size)}</td><td data-l="Mark">${esc(r.mark)}</td>
      <td data-l="Notional">${esc(r.notional)}</td><td data-l="Leverage">${esc(r.leverage)}</td>
      <td data-l="Stop / take" class="ex-trig">${[r.stop && [r.stop, "stop"], r.take && [r.take, "take"]].filter(Boolean).map(([t, k]) =>
        `<span>${esc(t)} <button type="button" class="ex-x" data-clear="${r.asset}:${k}"${st.flight.busy ? " disabled" : ""} aria-label="Remove ${k}" title="Remove ${k}">×</button></span>`).join("") || "—"}</td>
      <td data-l=""><button type="button" class="ex-cl" data-close="${r.asset}"${st.flight.busy ? " disabled" : ""}>Close</button></td></tr>`).join("")}</tbody></table>`
    : `<p class="ex-pad ex-muted">No open positions.</p>`) + `<p class="ex-exit">${esc(SCREEN_TEXT.exit)}</p>`;
}

function paintTicket(page, st) {
  const { reads, perAsset, block, now, account } = st.snap;
  const perp = st.perps.get(st.asset);
  const send = $("#ex-send", page);
  send.textContent = st.isBuy ? "Buy / Long" : "Sell / Short";
  send.className = `ex-go ${st.isBuy ? "b" : "s"}`;
  paintHead(page, st);
  if (!isKnown(perp)) {
    $("#ex-preview", page).innerHTML = `<p class="ex-warn bad">${esc(perp ? SCREEN_TEXT.noAsset : "The asset list of this network was not read, so no size can be taken.")}</p>`;
    send.disabled = true;
    for (const kind of ["stop", "take"]) {
      $(`#ex-${kind}-prev`, page).innerHTML = "";
      $(`#ex-${kind}-set`, page).disabled = true;
    }
    st.order = null;
    st.triggers = {};
    return;
  }
  const prices = perAsset.find((a) => a.asset === st.asset);
  const form = orderForm(reads, { asset: st.asset, perp, isBuy: st.isBuy, sizeInput: $("#ex-size", page).value,
    prices: { oracle: prices.oracle, bid: prices.bid, ask: prices.ask }, now, block });
  const view = previewView(form.preview, perp);
  const parts = [];
  if (form.error) parts.push(`<p class="ex-warn bad">${esc(form.error)}</p>`);
  if (view) {
    if (view.rows.length) parts.push(`<dl>${view.rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>`);
    parts.push(...view.warnings.map((w) => `<p class="ex-warn ${w.tone}">${esc(w.text)}</p>`));
    parts.push(`<p class="ex-note">${esc(view.note ?? ESTIMATE_NOTE)}</p>`);
  } else if (!form.error) {
    parts.push(`<p class="ex-note">${esc(ESTIMATE_NOTE)}</p>`);
  }
  $("#ex-preview", page).innerHTML = parts.join("");
  st.order = form;
  send.disabled = st.flight.busy || !account.canPlace || form.size === null || !view || view.blocked;
  const by = agentActive(st) ? "agent" : "trader";
  $("#ex-signs", page).textContent = `Signed by ${placeSigns(st, by)}. ${SCREEN_TEXT.fillsLater}`;

  for (const kind of ["stop", "take"]) {
    const tf = triggerForm(reads, { asset: st.asset, perp, isTake: kind === "take", pxInput: $(`#ex-${kind}-px`, page).value,
      sizeInput: $(`#ex-${kind}-sz`, page).value, oracle: prices.oracle, now, block });
    const tv = triggerPreviewView(tf.preview, perp);
    $(`#ex-${kind}-prev`, page).innerHTML = (tf.error ? `<p class="ex-warn bad">${esc(tf.error)}</p>` : "")
      + (tv ? tv.lines.map((l) => `<p class="ex-warn ${l.tone}">${esc(l.text)}</p>`).join("") : "");
    st.triggers[kind] = tf;
    $(`#ex-${kind}-set`, page).disabled = st.flight.busy || tf.px === null || !tv || tv.blocked;
  }
}

function placeSigns(st, by) {
  return placeCall({ exam: st.address, asset: st.asset, isBuy: true, size: 1n, by, keccak256: keccak }).signs;
}

function paintBook(page, st) {
  const b = st.book;
  const level = (l, side) => `<div class="ex-lv ${side}"><i style="width:${l.share}%"></i><span>${esc(l.px)}</span><span>${esc(l.sz)}</span><span>${esc(l.total.toFixed(3))}</span></div>`;
  const unit = st.perps.get(st.asset)?.name ?? "";
  $("#ex-book", page).innerHTML = b.empty ? "The book is empty." : `
    <div class="ex-bkh"><span>Price</span><span>Size (${esc(unit)})</span><span>Total</span></div>
    ${b.asks.map((l) => level(l, "ask")).join("")}
    <div class="ex-spr"><span>Spread</span><span>${esc(b.spreadAbs)}</span><span>${esc(b.spread)}</span></div>
    ${b.bids.map((l) => level(l, "bid")).join("")}`;
}

function paintJournal(page, st) {
  const rows = journalRows(st.journal, st.perps);
  $("#ex-journal", page).innerHTML = rows.length
    ? `<table class="ex-t ex-jr"><thead><tr><th>Time (UTC)</th><th>Event</th><th>Asset</th><th>Side</th><th>Size</th><th>Price</th><th>Cost</th><th>Price from</th><th>Note</th></tr></thead><tbody>
      ${[...rows].reverse().map((r) => `<tr><td>${esc(st.times.has(r.block) ? timeText(st.times.get(r.block)) : `block ${r.block}`)}</td>
        ${["event", "asset", "side", "size", "price", "cost", "from", "note"].map((k) => `<td>${esc(r.cells[k])}</td>`).join("")}</tr>`).join("")}</tbody></table>`
    : `<p class="ex-pad ex-muted">Nothing loaded yet.</p>`;
}

// ── the second key ──────────────────────────────────────────────

function secondKey(st) {
  const pk = store(keyName(st.address)) || kept.get(st.address) || null;
  if (!pk) return null;
  try {
    return new ethers.Wallet(pk);
  } catch {
    return null;
  }
}

function agentActive(st) {
  const key = secondKey(st);
  return Boolean(key && st.snap && same(st.snap.agent, key.address));
}

function paintAgent(page, st) {
  const key = secondKey(st);
  const onChain = st.snap.agent && !same(st.snap.agent, ZERO) ? st.snap.agent : null;
  const box = $("#ex-agent", page);
  const parts = [];
  if (key && same(onChain, key.address)) {
    parts.push(`<p>Second key on: <span class="ex-addr">${esc(key.address)}</span>. Orders from this browser are signed by it, with no prompts.</p>`,
      `<p class="ex-actions"><button type="button" class="ex-mini2" data-agent="off">Turn off</button> <button type="button" class="ex-mini2" data-agent="forget">Forget the key in this browser</button></p>`);
  } else if (key) {
    parts.push(`<p>A second key is kept in this browser: <span class="ex-addr">${esc(key.address)}</span>. It is not named on this exam yet.</p>`,
      `<p class="ex-actions"><button type="button" class="ex-mini2" data-agent="name">Name it on this exam</button> <button type="button" class="ex-mini2" data-agent="forget">Forget the key</button></p>`);
    if (onChain) parts.push(`<p class="ex-muted">Another second key is named on this exam: <span class="ex-addr">${esc(onChain)}</span>.</p>`);
  } else {
    parts.push(onChain ? `<p>A second key is named on this exam (<span class="ex-addr">${esc(onChain)}</span>) that this browser does not hold.</p>
      <p class="ex-actions"><button type="button" class="ex-mini2" data-agent="off">Turn off</button></p>` : "<p>No second key.</p>",
      `<p class="ex-actions"><button type="button" class="ex-mini2" data-agent="create">Create a second key in this browser</button></p>`);
  }
  box.innerHTML = parts.join("");
  $("#ex-tg", page).classList.toggle("on", Boolean(key && same(onChain, key.address)));
  if (key) {
    chain.readProvider.getBalance(key.address).then((b) => {
      const hint = $("#ex-agent", page);
      if (hint && !hint.querySelector(".ex-gas")) hint.insertAdjacentHTML("beforeend", `<p class="ex-muted ex-gas">Its balance: ${esc(ethers.formatEther(b))} HYPE</p>`);
    }).catch(() => {});
  }
}

// ── sending ─────────────────────────────────────────────────────

async function signerFor(st, signer) {
  if (signer === "agent") {
    const key = secondKey(st);
    if (!key) throw new Error("The second key is not in this browser.");
    return new ethers.Wallet(key.privateKey, chain.readProvider);
  }
  const s = chain.currentSigner() || (await chain.connect(), chain.currentSigner());
  if (!same(s.address, st.trader)) throw new Error("Connect the wallet that bought this exam: only its trader can do this.");
  return s;
}

async function sendCall(page, st, call, out) {
  // One call at a time: a second press while one is out does nothing, and only the call that set `busy` clears it.
  await st.flight.run(async () => {
    repaint(page, st);
    const el = $(out, page);
    el.className = "result busy";
    el.textContent = "Waiting for the signature…";
    try {
      const signer = await signerFor(st, call.signer);
      const tx = await signer.sendTransaction({ to: call.to, data: call.data });
      el.textContent = "Sent. Waiting for the block…";
      const receipt = await tx.wait();
      chain.forgetLiveReads();
      if (!receipt || receipt.status !== 1) throw new Error(`${call.method} reverted`);
      el.className = "result ok";
      el.textContent = call.method === "place" ? "Order sent. It is filled a few blocks later." : "Done.";
    } catch (err) {
      el.className = "result err";
      el.textContent = sendFailure(err, (d) => refusalOf(d, keccak));
    }
  });
  await tick(page, st).catch(() => {});
}

/** Paints what depends on a call being out: the buttons that send. */
function repaint(page, st) {
  if (!st.snap) return;
  paintAccount(page, st);
  paintPositions(page, st);
  paintTicket(page, st);
}

// ── events ──────────────────────────────────────────────────────

function wire(page, st) {
  const choose = () => { paintTicket(page, st); };
  const setAsset = (asset) => {
    st.asset = asset;
    st.rows = [];
    st.book = null;
    $("#ex-book", page).textContent = "Loading…";
    paintAssets(page, st);
    drawChart(page, st);
    refreshCandles(page, st);
    refreshBook(page, st);
    choose();
  };
  const on = (list, picked) => list.forEach((x) => x.classList.toggle("on", x === picked));
  page.addEventListener("change", (e) => { if (e.target.id === "ex-asset") setAsset(Number(e.target.value)); });
  page.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || !page.contains(b)) return;
    if (b.dataset.asset !== undefined) {
      setAsset(Number(b.dataset.asset));
    } else if (b.dataset.interval) {
      st.interval = b.dataset.interval;
      on([...page.querySelectorAll("[data-interval]")], b);
      refreshCandles(page, st);
    } else if (b.dataset.phone) {
      $("#ex-term", page).dataset.phone = b.dataset.phone;
      on([...page.querySelectorAll("[data-phone]")].filter((x) => x.tagName === "BUTTON"), b);
      drawChart(page, st);
    } else if (b.dataset.tab) {
      st.tab = b.dataset.tab;
      on([...page.querySelectorAll("[data-tab]")], b);
      $("#ex-positions", page).hidden = st.tab !== "pos";
      $("#ex-journal-pane", page).hidden = st.tab !== "jrnl";
    } else if (b.dataset.side) {
      st.isBuy = b.dataset.side === "buy";
      page.querySelectorAll("[data-side]").forEach((x) => { x.classList.toggle("on", x === b); });
      choose();
    } else if (b.id === "ex-send") {
      if (!st.order || st.order.size === null) return;
      const call = placeCall({ exam: st.address, asset: st.asset, isBuy: st.isBuy, size: st.order.size,
        by: agentActive(st) ? "agent" : "trader", keccak256: keccak });
      sendCall(page, st, call, "#ex-send-out");
    } else if (b.dataset.close !== undefined) {
      const asset = Number(b.dataset.close);
      const row = st.snap.account.positions.find((p) => p.asset === asset);
      if (!row) return;
      const r = positionRow(row, st.perps);
      const call = placeCall({ exam: st.address, asset, isBuy: r.closeIsBuy, size: r.closeSize, by: agentActive(st) ? "agent" : "trader", keccak256: keccak });
      sendCall(page, st, call, "#ex-send-out");
    } else if (b.dataset.set) {
      const t = st.triggers[b.dataset.set];
      if (!t || t.px === null) return;
      const call = setTriggerCall({ exam: st.address, asset: st.asset, isTake: b.dataset.set === "take", px: t.px, size: t.size,
        by: agentActive(st) ? "agent" : "trader", keccak256: keccak });
      sendCall(page, st, call, "#ex-send-out");
    } else if (b.dataset.clear) {
      const [asset, kind] = b.dataset.clear.split(":");
      sendCall(page, st, clearTriggerCall({ exam: st.address, asset: Number(asset), isTake: kind === "take", by: agentActive(st) ? "agent" : "trader", keccak256: keccak }), "#ex-send-out");
    } else if (b.id === "ex-graduate") {
      sendCall(page, st, graduateCall({ exam: st.address, by: agentActive(st) ? "agent" : "trader", keccak256: keccak }), "#ex-graduate-out");
    } else if (b.dataset.agent) {
      agentAction(page, st, b.dataset.agent);
    } else if (b.id === "ex-journal-load") {
      loadJournal(page, st, "earlier");
    } else if (b.id === "ex-journal-new") {
      loadJournal(page, st, "newer");
    }
  });
  page.addEventListener("input", (e) => { if (e.target.matches("#ex-size, #ex-stop-px, #ex-stop-sz, #ex-take-px, #ex-take-sz")) paintTicket(page, st); });
  const canvas = $("#ex-canvas", page);
  if (canvas) {
    canvas.addEventListener("mousemove", (e) => {
      const r = canvas.getBoundingClientRect();
      st.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
      drawChart(page, st);
    });
    canvas.addEventListener("mouseleave", () => { st.hover = null; drawChart(page, st); });
    if (window.ResizeObserver) new ResizeObserver(() => drawChart(page, st)).observe(canvas.parentElement);
  }
}

async function agentAction(page, st, what) {
  const out = $("#ex-graduate-out", page);
  if (what === "create") {
    const wallet = ethers.Wallet.createRandom();
    kept.set(st.address, wallet.privateKey);
    store(keyName(st.address), wallet.privateKey);
    paintAgent(page, st);
  } else if (what === "forget") {
    kept.delete(st.address);
    store(keyName(st.address), null);
    paintAgent(page, st);
  } else {
    const key = secondKey(st);
    const agent = what === "name" ? key?.address : ZERO;
    if (!agent) return;
    await sendCall(page, st, setAgentCall({ exam: st.address, agent, filler: st.snap.reads.filler, keccak256: keccak }), "#ex-send-out");
  }
  out.textContent = "";
}

/** The time of each block the journal names, up to 40 of them; a row without one says its block. */
async function blockTimes(st, blocks) {
  const wanted = [...new Set(blocks)].filter((n) => !st.times.has(n)).slice(-40);
  await Promise.all(wanted.map((n) => attempt(async () => {
    const b = await chain.readProvider.getBlock(n);
    if (b) st.times.set(n, b.timestamp);
  })));
}

/** Reads the next range of the journal (`journalPlan`): the newest blocks at first, then older ones or the ones since the
 *  top of what is loaded. What the note says is what was read: the range joins what was there, and a read that failed
 *  changes neither the rows nor the range. */
async function loadJournal(page, st, direction = "earlier") {
  const buttons = [$("#ex-journal-load", page), $("#ex-journal-new", page)];
  const info = $("#ex-journal-note", page);
  buttons.forEach((x) => { x.disabled = true; });
  try {
    const plan = journalPlan({ latest: st.snap.block, loaded: st.loaded, direction });
    if (!plan) {
      info.textContent = direction === "newer" ? "Nothing newer yet." : "That is the start of the chain.";
      return;
    }
    const { logs } = await readLogs({ from: plan.from, to: plan.to, window: JOURNAL_WINDOW, maxCalls: JOURNAL_CALLS,
      getLogs: (x, y) => chain.readProvider.getLogs({ address: st.address, fromBlock: x, toBlock: y }) });
    const events = logs.map((l) => {
      const parsed = st.exam.interface.parseLog(l);
      return parsed ? { name: parsed.name, args: parsed.args, block: l.blockNumber, index: l.index } : null;
    }).filter(Boolean);
    st.journal = [...events, ...st.journal].sort((p, q) => p.block - q.block || p.index - q.index);
    st.loaded = st.loaded ? { from: Math.min(st.loaded.from, plan.from), top: Math.max(st.loaded.top, plan.to) } : { from: plan.from, top: plan.to };
    await blockTimes(st, events.map((e) => e.block));
    info.textContent = journalRange(st.loaded);
    paintJournal(page, st);
    buttons[0].textContent = "Load earlier events";
    buttons[1].hidden = false;
  } catch (err) {
    info.textContent = `Could not load: ${friendly(err)}`;
  } finally {
    buttons.forEach((x) => { x.disabled = false; });
  }
}

// ── the chart: candles drawn on a canvas, no library ────────────

function colors(page) {
  const css = getComputedStyle(page);
  const get = (n, fallback) => css.getPropertyValue(n).trim() || fallback;
  return { up: get("--up", "#2F7FC9"), down: get("--dn", "#C0622A"), text: get("--mute", "#5E6670"), grid: get("--line", "#D6D8D6"), ink: get("--ink", "#0E1116"),
    vol: "rgba(31,53,80,.12)" };
}

function drawChart(page, st) {
  const canvas = $("#ex-canvas", page);
  if (!canvas) return;
  const wrap = canvas.parentElement;
  const width = Math.max(200, Math.floor(wrap.clientWidth));
  const height = Math.max(220, Math.floor(wrap.clientHeight));
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const c = colors(page);
  const rows = st.rows;
  ctx.font = "11px Inter, system-ui, sans-serif";
  if (!rows.length) {
    ctx.fillStyle = c.text;
    ctx.fillText("Loading candles…", 12, 20);
    return;
  }
  const dec = Math.max(0, 6 - (st.perps.get(st.asset)?.szDecimals ?? 0));
  const fmt = (v) => v.toFixed(Math.min(dec, v >= 1000 ? 1 : dec));
  const s = chartScale(rows, { width, height });
  const volTop = s.top + s.plotH * 0.86;
  const volMax = Math.max(...rows.map((k) => k.volume), 1);
  ctx.fillStyle = c.vol;
  rows.forEach((k, i) => {
    const h = (k.volume / volMax) * (s.plotH * 0.14);
    ctx.fillRect(s.x(i) - s.body / 2, s.top + s.plotH - h, s.body, h);
  });
  void volTop;
  ctx.lineWidth = 1;
  ctx.textBaseline = "middle";
  for (const t of s.ticks) {
    const y = Math.round(s.y(t)) + 0.5;
    ctx.strokeStyle = c.grid;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(s.plotW, y);
    ctx.stroke();
    ctx.fillStyle = c.text;
    ctx.fillText(fmt(t), s.plotW + 6, y);
  }
  rows.forEach((k, i) => {
    const x = s.x(i);
    const color = k.close >= k.open ? c.up : c.down;
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, s.y(k.high));
    ctx.lineTo(Math.round(x) + 0.5, s.y(k.low));
    ctx.stroke();
    const top = s.y(Math.max(k.open, k.close));
    ctx.fillRect(x - s.body / 2, top, s.body, Math.max(1, s.y(Math.min(k.open, k.close)) - top));
  });
  const last = rows[rows.length - 1];
  const ly = s.y(last.close);
  ctx.setLineDash([4, 3]);
  ctx.strokeStyle = c.ink;
  ctx.beginPath();
  ctx.moveTo(0, ly);
  ctx.lineTo(s.plotW, ly);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = c.ink;
  ctx.fillRect(s.plotW, ly - 9, s.axis, 18);
  ctx.fillStyle = "#fff";
  ctx.fillText(fmt(last.close), s.plotW + 6, ly);
  // the time axis: a label every few candles
  ctx.fillStyle = c.text;
  ctx.textBaseline = "alphabetic";
  const every = Math.max(1, Math.round(rows.length / Math.max(2, Math.floor(s.plotW / 90))));
  for (let i = 0; i < rows.length; i += every) {
    const d = new Date(rows[i].time * 1000);
    const label = st.interval === "1d" ? `${d.getUTCMonth() + 1}/${d.getUTCDate()}` : `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
    ctx.fillText(label, Math.max(0, s.x(i) - 12), height - 6);
  }
  const hv = st.hover;
  if (hv && hv.x < s.plotW) {
    const i = s.index(hv.x);
    ctx.strokeStyle = c.text;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(s.x(i), 0);
    ctx.lineTo(s.x(i), height - 20);
    ctx.moveTo(0, hv.y);
    ctx.lineTo(s.plotW, hv.y);
    ctx.stroke();
    ctx.setLineDash([]);
    $("#ex-hover", page).textContent = candleText(rows[i]);
  } else {
    $("#ex-hover", page).textContent = candleText(last);
  }
}
