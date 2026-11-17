// The trading page of a paper exam (#/exam/<exam address>): a chart and a book of Hyperliquid's open data, an order
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
  journalRows, sendFailure, timeLeft,
} from "../lib/examscreen.js";

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
    render(page, `<section class="card narrow"><h2>Paper exam</h2>
      <p>${esc(WHAT_THIS_IS)}</p>
      <p>Open the page from the exam's address:</p>
      <div class="inline"><input id="addr" placeholder="0x…" size="44"><button id="go">Open</button></div></section>`);
    $("#go", page).addEventListener("click", () => { location.hash = `#/exam/${$("#addr", page).value.trim()}`; });
    return;
  }
  if ((await chain.readProvider.getCode(address)) === "0x") {
    render(page, `<section class="card narrow"><h2>Paper exam</h2><p>There is no contract at this address on the network this page reads.</p>
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
  if (ds.ok) for (const a of assets) perps.set(a, await hl.perp(a));
  const st = {
    exam, address, ds, perps, assets, trader, asset: assets[0], isBuy: true, interval: "15m", snap: null, rows: [],
    book: null, hover: null, journal: [], journalFrom: null, journalComplete: false, busy: false, timers: [],
    kind: "stop", lastError: "",
  };

  render(page, skeleton(st));
  wire(page, st);
  // The first reading decides whether the page can be drawn: a failure here is the page's failure, not a panel's.
  await tick(page, st);
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
  const intervals = INTERVALS.map((i) => `<button type="button" class="secondary small" data-interval="${i.key}"${i.key === st.interval ? ' aria-pressed="true"' : ""}>${i.key}</button>`).join("");
  return `
    ${ds.ok && ds.rehearsal ? `<div class="ex-rehearsal" role="status">${esc(SCREEN_TEXT.rehearsal)}</div>` : ""}
    <section class="card ex-strip">
      <div class="ex-strip-head"><span class="badge ex-badge">${esc(SCREEN_TEXT.strip)}</span><p class="small">${esc(WHAT_THIS_IS)}</p></div>
      <div class="ex-figures" id="ex-figures"></div>
    </section>
    <p class="notice" id="ex-error" role="status" hidden></p>
    ${ds.ok ? "" : `<section class="card"><h3>Chart and book not shown</h3><p>${esc(ds.text)}</p></section>`}
    <div class="ex-grid">
      <section class="card ex-chart"${ds.ok ? "" : " hidden"}>
        <div class="ex-chart-head"><div class="ex-tabs" id="ex-assets" role="tablist" aria-label="Asset"></div>
          <div class="ex-intervals" role="group" aria-label="Interval">${intervals}</div></div>
        <div class="ex-canvas-wrap"><canvas id="ex-canvas" role="img" aria-label="Candle chart"></canvas></div>
        <p class="muted small ex-hover" id="ex-hover">&nbsp;</p>
        <p class="muted small">${esc(SCREEN_TEXT.chart)}</p>
      </section>
      <section class="card ex-book"${ds.ok ? "" : " hidden"}>
        <h4>Order book</h4>
        <div id="ex-book" class="muted">Loading…</div>
        <p class="muted small">${esc(SCREEN_TEXT.book)}</p>
        <p class="muted small">${esc(SCREEN_TEXT.bookFill)}</p>
      </section>
      <section class="card ex-ticket">
        <h4>Order</h4>
        <div class="ex-sides" role="group" aria-label="Side">
          <button type="button" class="ex-buy" data-side="buy" aria-pressed="true">Buy / Long</button>
          <button type="button" class="ex-sell secondary" data-side="sell" aria-pressed="false">Sell / Short</button>
        </div>
        <label class="ex-field"><span>Size <b id="ex-unit"></b></span><input id="ex-size" inputmode="decimal" autocomplete="off" placeholder="0.00"></label>
        <div id="ex-preview" class="ex-preview"></div>
        <button type="button" id="ex-send" class="ex-submit" disabled>Buy / Long</button>
        <p class="muted small" id="ex-signs"></p>
        <p class="result" id="ex-send-out"></p>
        <div id="ex-pending" class="ex-pending" hidden></div>
        <details class="ex-trigger" open>
          <summary>Stop or take on a position</summary>
          <div class="ex-sides ex-kind" role="group" aria-label="Kind">
            <button type="button" data-kind="stop" aria-pressed="true" class="small">Stop</button>
            <button type="button" data-kind="take" aria-pressed="false" class="secondary small">Take</button>
          </div>
          <label class="ex-field"><span>Price</span><input id="ex-tpx" inputmode="decimal" autocomplete="off" placeholder="price"></label>
          <label class="ex-field"><span>Size to close (empty: all)</span><input id="ex-tsz" inputmode="decimal" autocomplete="off" placeholder="all"></label>
          <div id="ex-tpreview" class="ex-preview"></div>
          <button type="button" id="ex-tset" disabled>Set stop</button>
          <p class="result" id="ex-tset-out"></p>
        </details>
      </section>
      <section class="card ex-tables">
        <h4>Positions</h4>
        <div id="ex-positions" class="scroll muted">Loading…</div>
        <h4 class="ex-gap">Journal</h4>
        <div id="ex-journal" class="muted">Not loaded.</div>
        <p class="actions"><button type="button" class="secondary small" id="ex-journal-load">Load recent events</button><span class="muted small" id="ex-journal-note"></span></p>
      </section>
      <section class="card ex-notes">
        <h4>How orders work here</h4>
        <ul class="small">
          <li>${esc(SCREEN_TEXT.fillsLater)}</li>
          <li>${esc(SCREEN_TEXT.exit)}</li>
          <li>${esc(SCREEN_TEXT.gas)}</li>
          <li>${esc(SCREEN_TEXT.noLimits)}</li>
          <li>${esc(SCREEN_TEXT.data)}</li>
        </ul>
      </section>
      <section class="card ex-account">
        <h4>Exam account</h4>
        <div id="ex-account" class="muted">Loading…</div>
        <div id="ex-scale"></div>
        <p><button type="button" id="ex-graduate" disabled>Pass the step</button></p>
        <p class="muted small" id="ex-graduate-text"></p>
        <p class="result" id="ex-graduate-out"></p>
        <h4 class="ex-gap">Trade without wallet prompts</h4>
        <p class="small">${esc(SCREEN_TEXT.secondKey)}</p>
        <div id="ex-agent"></div>
      </section>
    </div>`;
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
    `<button type="button" role="tab" class="secondary small" data-asset="${a}" aria-selected="${a === st.asset}">${esc(st.perps.get(a)?.name ?? `#${a}`)}</button>`).join("");
  $("#ex-unit", page).textContent = st.perps.get(st.asset)?.name ?? "";
}

function paintAccount(page, st) {
  const { account, now } = st.snap;
  const f = accountFigures(account);
  const stateBadge = `<span class="badge ${account.status === "active" ? "ok" : "bad"}">${esc(f.status)}</span>`;
  $("#ex-figures", page).innerHTML = `
    <div class="cell"><span class="cell-label">Exam size</span><span class="cell-value">${esc(f.size)}</span></div>
    <div class="cell"><span class="cell-label">Step</span><span class="cell-value">${esc(f.step)}</span></div>
    <div class="cell"><span class="cell-label">State</span><span class="cell-value">${stateBadge}</span></div>
    <div class="cell"><span class="cell-label">Equity</span><span class="cell-value">${esc(f.equity)}</span></div>`;
  const rows = [
    ["Equity", f.equity], ["Balance", f.balance], ["Leverage", f.leverage],
    ...(f.target ? [["Target", `${f.target.at} · ${f.target.toGo} to go`]] : []),
    ...f.lines.map((l) => [l.label, `${l.at} · room ${l.room}${l.near ? " · nearest" : ""}${l.from ? ` · ${l.from}` : ""}`]),
    ["Time left", account.time.endsAt === null ? "—" : timeLeft(account.time.endsAt, now)],
    ...(f.days ? [["Trading days", f.days]] : []),
  ];
  $("#ex-account", page).innerHTML = rows.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  const scale = accountScale(account);
  $("#ex-scale", page).innerHTML = scale ? `<div class="ex-scale${scale.broken ? " broken" : ""}" role="img" aria-label="Equity between the loss limits and the target">
      ${scale.lines.map((l) => `<i class="line${l.near ? " near" : ""}" style="left:${l.share / 100}%" title="${l.kind === "drawdown" ? "Loss limit" : "Daily loss limit"}"></i>`).join("")}
      <i class="target" style="left:${scale.target / 100}%" title="Target"></i>
      <b style="left:${scale.equity / 100}%" title="Equity"></b></div>
    <p class="muted small ex-scale-key">Loss limit ← equity → target</p>` : "";
  const g = $("#ex-graduate", page);
  g.disabled = !account.graduate.can || st.busy;
  $("#ex-graduate-text", page).textContent = account.graduate.can ? "" : account.graduate.text;
}

function paintPositions(page, st) {
  const { account } = st.snap;
  if (!st.ds.ok) {
    $("#ex-positions", page).textContent = "Positions are not shown: the asset list of this network was not read.";
    return;
  }
  const rows = account.positions.map((p) => positionRow(p, st.perps));
  const pending = pendingText(account.pending, st.perps);
  const box = $("#ex-pending", page);
  box.hidden = !pending;
  box.textContent = pending ? pending.text : "";
  const hasAny = rows.length > 0;
  $("#ex-positions", page).innerHTML = hasAny ? `<table><thead><tr><th>Asset</th><th>Side</th><th>Size</th><th>Oracle</th><th>Notional</th><th>Leverage</th><th>Stop / take</th><th></th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${esc(r.name)}</td><td>${esc(r.side)}</td><td>${esc(r.size)}</td><td>${esc(r.mark)}</td><td>${esc(r.notional)}</td><td>${esc(r.leverage)}</td>
      <td class="ex-trig">${[r.stop && [r.stop, "stop"], r.take && [r.take, "take"]].filter(Boolean).map(([t, k]) =>
        `<span>${esc(t)} <button type="button" class="secondary small" data-clear="${r.asset}:${k}">Remove ${k}</button></span>`).join("") || "—"}</td>
      <td><button type="button" class="small" data-close="${r.asset}">Close</button></td></tr>`).join("")}</tbody></table>`
    : "No open positions.";
  paintJournal(page, st);
}

function paintTicket(page, st) {
  const { reads, perAsset, block, now, account } = st.snap;
  const perp = st.perps.get(st.asset);
  const send = $("#ex-send", page);
  send.textContent = st.isBuy ? "Buy / Long" : "Sell / Short";
  send.className = `ex-submit ${st.isBuy ? "ex-buy" : "ex-sell"}`;
  if (!perp) {
    $("#ex-preview", page).textContent = "The asset list of this network was not read, so no size can be taken.";
    send.disabled = true;
    return;
  }
  const prices = perAsset.find((a) => a.asset === st.asset);
  const form = orderForm(reads, { asset: st.asset, perp, isBuy: st.isBuy, sizeInput: $("#ex-size", page).value,
    prices: { oracle: prices.oracle, bid: prices.bid, ask: prices.ask }, now, block });
  const view = previewView(form.preview, perp);
  const parts = [];
  if (form.error) parts.push(`<p class="ex-warn bad">${esc(form.error)}</p>`);
  if (view) {
    if (view.rows.length) parts.push(view.rows.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join(""));
    parts.push(...view.warnings.map((w) => `<p class="ex-warn ${w.tone}">${esc(w.text)}</p>`));
    parts.push(`<p class="muted small">${esc(view.note ?? ESTIMATE_NOTE)}</p>`);
  } else if (!form.error) {
    parts.push(`<p class="muted small">${esc(ESTIMATE_NOTE)}</p>`);
  }
  $("#ex-preview", page).innerHTML = parts.join("");
  st.order = form;
  send.disabled = st.busy || !account.canPlace || form.size === null || !view || view.blocked;
  const by = agentActive(st) ? "agent" : "trader";
  $("#ex-signs", page).textContent = `Signed by ${placeSigns(st, by)}`;

  const tf = triggerForm(reads, { asset: st.asset, perp, isTake: st.kind === "take", pxInput: $("#ex-tpx", page).value,
    sizeInput: $("#ex-tsz", page).value, oracle: prices.oracle, now, block });
  const tv = triggerPreviewView(tf.preview, perp);
  $("#ex-tpreview", page).innerHTML = (tf.error ? `<p class="ex-warn bad">${esc(tf.error)}</p>` : "")
    + (tv ? tv.lines.map((l) => `<p class="ex-warn ${l.tone}">${esc(l.text)}</p>`).join("") : "");
  st.trigger = tf;
  $("#ex-tset", page).disabled = st.busy || tf.px === null || !tv || tv.blocked;
  $("#ex-tset", page).textContent = st.kind === "take" ? "Set take" : "Set stop";
}

function placeSigns(st, by) {
  return placeCall({ exam: st.address, asset: st.asset, isBuy: true, size: 1n, by, keccak256: keccak }).signs;
}

function paintBook(page, st) {
  const b = st.book;
  const dec = (6 - (st.perps.get(st.asset)?.szDecimals ?? 0));
  const level = (l, side) => `<div class="ex-level ${side}"><i style="width:${l.share}%"></i><span>${esc(l.px)}</span><span>${esc(l.sz)}</span></div>`;
  $("#ex-book", page).innerHTML = b.empty ? "The book is empty." : `
    <div class="ex-level head"><span>Price</span><span>Size</span></div>
    ${b.asks.map((l) => level(l, "ask")).join("")}
    <div class="ex-spread">${b.mid === null ? "—" : esc(Number(b.mid.toFixed(dec)))} <span class="muted small">spread ${esc(b.spread)}</span></div>
    ${b.bids.map((l) => level(l, "bid")).join("")}`;
}

function paintJournal(page, st) {
  const rows = journalRows(st.journal, st.perps);
  $("#ex-journal", page).innerHTML = rows.length
    ? `<ul class="ex-log">${[...rows].reverse().map((r) => `<li><span class="muted small">block ${r.block}</span> ${esc(r.text)}</li>`).join("")}</ul>`
    : "Nothing loaded yet.";
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
    parts.push(`<p>Second key on: <span class="mono">${esc(key.address)}</span>. Orders from this browser are signed by it, with no prompts.</p>`,
      `<p class="actions"><button type="button" class="secondary" data-agent="off">Turn off</button> <button type="button" class="secondary" data-agent="forget">Forget the key in this browser</button></p>`);
  } else if (key) {
    parts.push(`<p>A second key is kept in this browser: <span class="mono">${esc(key.address)}</span>. It is not named on this exam yet.</p>`,
      `<p class="actions"><button type="button" data-agent="name">Name it on this exam</button> <button type="button" class="secondary" data-agent="forget">Forget the key</button></p>`);
    if (onChain) parts.push(`<p class="muted small">Another second key is named on this exam: <span class="mono">${esc(onChain)}</span>.</p>`);
  } else {
    parts.push(onChain ? `<p>A second key is named on this exam (<span class="mono">${esc(onChain)}</span>) that this browser does not hold.</p>
      <p class="actions"><button type="button" class="secondary" data-agent="off">Turn off</button></p>` : "<p>No second key.</p>",
      `<p class="actions"><button type="button" data-agent="create">Create a second key in this browser</button></p>`);
  }
  box.innerHTML = parts.join("");
  if (key) {
    chain.readProvider.getBalance(key.address).then((b) => {
      const hint = $("#ex-agent", page);
      if (hint && !hint.querySelector(".ex-gas")) hint.insertAdjacentHTML("beforeend", `<p class="muted small ex-gas">Its balance: ${esc(ethers.formatEther(b))} HYPE</p>`);
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
  st.busy = true;
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
  } finally {
    st.busy = false;
    await tick(page, st).catch(() => {});
  }
}

// ── events ──────────────────────────────────────────────────────

function wire(page, st) {
  const choose = () => { paintTicket(page, st); };
  page.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || !page.contains(b)) return;
    if (b.dataset.asset !== undefined) {
      st.asset = Number(b.dataset.asset);
      st.rows = [];
      st.book = null;
      $("#ex-book", page).textContent = "Loading…";
      paintAssets(page, st);
      drawChart(page, st);
      refreshCandles(page, st);
      refreshBook(page, st);
      choose();
    } else if (b.dataset.interval) {
      st.interval = b.dataset.interval;
      page.querySelectorAll("[data-interval]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      refreshCandles(page, st);
    } else if (b.dataset.side) {
      st.isBuy = b.dataset.side === "buy";
      page.querySelectorAll("[data-side]").forEach((x) => { x.setAttribute("aria-pressed", String(x === b)); x.classList.toggle("secondary", x !== b); });
      choose();
    } else if (b.dataset.kind) {
      st.kind = b.dataset.kind;
      page.querySelectorAll("[data-kind]").forEach((x) => { x.setAttribute("aria-pressed", String(x === b)); x.classList.toggle("secondary", x !== b); });
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
    } else if (b.id === "ex-tset") {
      if (!st.trigger || st.trigger.px === null) return;
      const call = setTriggerCall({ exam: st.address, asset: st.asset, isTake: st.kind === "take", px: st.trigger.px, size: st.trigger.size,
        by: agentActive(st) ? "agent" : "trader", keccak256: keccak });
      sendCall(page, st, call, "#ex-tset-out");
    } else if (b.dataset.clear) {
      const [asset, kind] = b.dataset.clear.split(":");
      sendCall(page, st, clearTriggerCall({ exam: st.address, asset: Number(asset), isTake: kind === "take", by: agentActive(st) ? "agent" : "trader", keccak256: keccak }), "#ex-send-out");
    } else if (b.id === "ex-graduate") {
      sendCall(page, st, graduateCall({ exam: st.address, by: agentActive(st) ? "agent" : "trader", keccak256: keccak }), "#ex-graduate-out");
    } else if (b.dataset.agent) {
      agentAction(page, st, b.dataset.agent);
    } else if (b.id === "ex-journal-load") {
      loadJournal(page, st);
    }
  });
  page.addEventListener("input", (e) => { if (e.target.matches("#ex-size, #ex-tpx, #ex-tsz")) paintTicket(page, st); });
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

async function loadJournal(page, st) {
  const btn = $("#ex-journal-load", page);
  btn.disabled = true;
  const info = $("#ex-journal-note", page);
  try {
    const latest = st.snap.block;
    const to = st.journalFrom === null ? latest : st.journalFrom - 1;
    const from = Math.max(0, to - JOURNAL_WINDOW * JOURNAL_CALLS + 1);
    const { logs, readTo } = await readLogs({ from, to, window: JOURNAL_WINDOW, maxCalls: JOURNAL_CALLS,
      getLogs: (a, b) => chain.readProvider.getLogs({ address: st.address, fromBlock: a, toBlock: b }) });
    const events = logs.map((l) => {
      const parsed = st.exam.interface.parseLog(l);
      return parsed ? { name: parsed.name, args: parsed.args, block: l.blockNumber, index: l.index } : null;
    }).filter(Boolean);
    st.journal = [...events, ...st.journal].sort((x, y) => x.block - y.block || x.index - y.index);
    st.journalFrom = from;
    info.textContent = `Events from block ${from} to ${latest}.`;
    paintJournal(page, st);
    btn.textContent = "Load earlier events";
  } catch (err) {
    info.textContent = `Could not load: ${friendly(err)}`;
  } finally {
    btn.disabled = false;
  }
}

// ── the chart: candles drawn on a canvas, no library ────────────

function colors() {
  const css = getComputedStyle(document.body);
  const get = (n, fallback) => css.getPropertyValue(n).trim() || fallback;
  return { up: get("--ok", "#1a7f37"), down: get("--bad", "#b42318"), text: get("--muted", "#3E5068"), grid: get("--line", "#C7E1F9"), ink: get("--text", "#16181D") };
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
  const c = colors();
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
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = last.close >= last.open ? c.up : c.down;
  ctx.beginPath();
  ctx.moveTo(0, ly);
  ctx.lineTo(s.plotW, ly);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = last.close >= last.open ? c.up : c.down;
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
