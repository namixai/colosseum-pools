// The trading page of an exam (#/exam/<exam address>): a chart and a book of Hyperliquid's open data, an order
// form, the waiting order, the positions with their stops and takes, the account against its lines, and the journal.
// An order here is a transaction to the trader's own exam contract (PaperExam.place), not an order on any exchange.
// Every sum is app/lib/exam.js's; what is said and where a number sits is app/lib/examscreen.js; this file reads, draws
// and sends. It is not app/views/trading.js, which is another product's page.
import * as chain from "../lib/chain.js";
import * as hl from "../lib/hl.js";
import { CONFIG } from "../config.js";
import { esc, render, $, isAddress, friendly } from "../lib/ui.js";
import { readLogs, SIM_POOL_ABI } from "../lib/simpool.js";
import { ACCOUNT_ABI, KIND } from "../lib/simtrader.js";
import { userHeaderHtml, footerHtml } from "../lib/simpanel.js";
import { accountLabel, wireShell } from "./simshell.js";
import {
  PAPER_EXAM_ABI, dataSource, examAccount, oraclePxCall, bboCall, placeCall, graduateCall, setAgentCall,
  setTriggerCall, clearTriggerCall, refusalOf, settleCall, STOP_BOOK_ABI,
} from "../lib/exam.js";
import {
  SCREEN_TEXT, WHAT_THIS_IS, ESTIMATE_NOTE, BOOK_DEPTH, readsFrom, accountFigures, positionRow,
  pendingText, accountScale, orderForm, previewView, triggerForm, triggerPreviewView, bookView,
  journalRows, sendFailure, timeLeft, networkMarks, lifeSteps, timeText, markUnknown, isKnown, singleFlight, journalPlan, journalRange, canLoadEarlier,
  assetFromQuery, queryWithAsset, marketRows, stepIndex, sizeUnitOf, sizeEntry,
} from "../lib/examscreen.js";
import { createExamChart } from "./examchart.js";
import { positionLines } from "../lib/positionlines.js";
import { createEntryTracker, browserStore } from "../lib/entrytrack.js";
import { pxText } from "../lib/exam.js";
import { readPass, readList, fullyRead, listPrices, withPrices, createPasses, shownPrice, STALE_PASS_POLLS, LIST_MS } from "../lib/examreads.js";

const { ethers } = window;
const POLL_MS = 5000;
const BOOK_MS = 4000;
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
const UNIT_KEY = "exam-size-unit";
const kept = new Map();  // the second key where storage is blocked, for this tab

/** The pool an exam belongs to, when it is an exam or a stage of a pool of exams (the pool's own `accountOf` says): the
 *  trading screen then stands in that pool's user panel. Null for an account of any other kind of pool. */
async function simPoolOf(address) {
  try {
    const pool = await new ethers.Contract(address, ACCOUNT_ABI, chain.readProvider).pool();
    if (!isAddress(pool) || same(pool, ZERO)) return null;
    const account = await new ethers.Contract(pool, SIM_POOL_ABI, chain.readProvider).accountOf(address);
    return [KIND.exam, KIND.stage].includes(Number(account.kind)) ? ethers.getAddress(pool) : null;
  } catch {
    return null;
  }
}

/** The site's own header and footer come back: this page is not drawn in the user panel. */
const leavePanel = (page) => { if (page.isConnected) delete document.body.dataset.panel; };

export async function examView(address, page, query = "") {
  if (!isAddress(address)) {
    leavePanel(page);
    render(page, `<section class="card narrow"><h2>Exam</h2>
      <p>${esc(WHAT_THIS_IS)}</p>
      <p>Open the page from the exam's address:</p>
      <div class="inline"><input id="addr" placeholder="0x…" size="44"><button id="go">Open</button></div></section>`);
    $("#go", page).addEventListener("click", () => { location.hash = `#/exam/${$("#addr", page).value.trim()}`; });
    return;
  }
  if ((await chain.readProvider.getCode(address)) === "0x") {
    leavePanel(page);
    render(page, `<section class="card narrow"><h2>Exam</h2><p>There is no contract at this address on the network this page reads.</p>
      <p><a href="#/exam">Back</a></p></section>`);
    return;
  }
  const exam = new ethers.Contract(address, PAPER_EXAM_ABI, chain.readProvider);
  // The chain the exam is on is the one the node that answers its getters says (eth_chainId): the page's data source
  // is that chain's or nothing (dataSource), never another network's candles beside this exam.
  const chainId = await nodeChainId();
  const ds = dataSource({ chainId }, CONFIG);
  // The stop book of the exam's factory, where it has one: an exam made before the book has no `stopBook` getter.
  const [rules, terms, trader, panelPool, bookAddress] = await Promise.all([exam.rules(), exam.terms(), exam.trader(),
    simPoolOf(address), exam.stopBook().catch(() => null)]);
  const stopBook = bookAddress && !/^0x0{40}$/i.test(bookAddress)
    ? new ethers.Contract(bookAddress, STOP_BOOK_ABI, chain.readProvider) : null;
  // The panel's header and footer (set by the router for every exam address) stay only for an exam of a pool of exams.
  // This continuation may have been left behind by a page change: only the page on screen touches the page-wide marker.
  if (!page.isConnected) return;
  if (!panelPool) leavePanel(page);
  const assets = [...rules.assets].map(Number);
  const perps = new Map();
  if (ds.ok) for (const a of assets) perps.set(a, markUnknown(await hl.perp(a), a));
  const st = {
    exam, address, ds, perps, assets, trader, panelPool, stopBook, bookAddress: stopBook ? bookAddress : null, asset: assetFromQuery(query, assets), query,
    unit: sizeUnitOf(store(UNIT_KEY)), market: { open: false, q: "", at: -1 }, isBuy: true, snap: null, prices: new Map(), listSession: 0, listReading: null, passes: createPasses({ staleMs: STALE_PASS_POLLS * POLL_MS }), chart: null,
    book: null, journal: [], timers: [],
    times: new Map(), tab: "pos", triggers: {}, flight: singleFlight(), loaded: null,
  };

  if (panelPool) {
    // The user panel's header and footer, the menu on Trade; the screen itself is the same one.
    const marks = networkMarks(CONFIG);
    page.className = "ex-wrap";
    render(page, `<div class="sp sp-user">${userHeaderHtml({ address: panelPool, account: accountLabel(), current: "trade", route: `#/exam/${address}`, marks })}`
      + `<div class="ex-page">${skeleton(st)}</div>`
      + `${footerHtml({ marks, source: "The account is read from the exam's contract. Prices and the book are Hyperliquid's." })}</div>`);
    wireShell(page);
  } else {
    page.className = "ex-page";
    render(page, skeleton(st));
  }
  wire(page, st);
  paintAssets(page, st);
  // The first reading decides whether the page can be drawn: a failure here is the page's failure, not a panel's.
  await tick(page, st);
  paintJournal(page, st);
  if (ds.ok) {
    st.chart = createExamChart({ node: $("#ex-chart", page), bar: $("#ex-iv", page), load: hl.candles, explain: friendly });
    st.chart.setAsset(chartAsset(st));
    // The entry price of an open position is proven from the exam's logs in the background, after the screen is drawn.
    st.entries = createEntryTracker({ address, read: (from, to) => readEntryEvents(st, from, to), store: browserStore(), onChange: () => paintHead(page, st) });
    trackEntry(st);
    paintHead(page, st);
    refreshBook(page, st);
    st.timers.push(setInterval(() => refreshBook(page, st), BOOK_MS));
  }
  // A pass still out is let to finish: a poll that started another would pass it by, and a pass longer than the interval
  // would never be drawn.
  st.timers.push(setInterval(() => {
    if (st.passes.running) return;
    tick(page, st).catch((err) => note(page, `Could not refresh: ${friendly(err)}`));
  }, POLL_MS));
  // The list's prices of the markets a pass does not read: only while the list is open.
  st.timers.push(setInterval(() => refreshList(page, st).catch(() => {}), LIST_MS));
  // A page that was left stops its readings: it is no longer in the document.
  st.timers.push(setInterval(() => {
    if (page.isConnected) return;
    st.timers.forEach(clearInterval);
    st.chart?.destroy();
    st.entries?.stop();
  }, 1000));
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
  const trigger = (k, label) => `<div class="ex-tr"><label>Set ${label}<input id="ex-${k}-px" inputmode="decimal" autocomplete="off" placeholder="price"></label>
      <label>Size<input id="ex-${k}-sz" inputmode="decimal" autocomplete="off" placeholder="all"></label>
      <button type="button" class="ex-mini2" id="ex-${k}-set" data-set="${k}" disabled>Set ${label}</button></div>
      <div class="ex-prev" id="ex-${k}-prev"></div>`;
  return `
    <div class="ex-strip">
      <span class="ex-badge">${esc(SCREEN_TEXT.strip)}</span>${marks && !st.panelPool ? `<span class="ex-tag">${esc(marks.tag)}</span>` : ""}
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
          <div class="ex-mk">
            <button type="button" id="ex-market" class="ex-mkbtn" aria-haspopup="listbox" aria-expanded="false" aria-controls="ex-pop"><b id="ex-market-name">—</b><i aria-hidden="true">▾</i></button>
            <div class="ex-pop" id="ex-pop" hidden>
              <div class="ex-pophead"><input id="ex-mq" type="search" autocomplete="off" spellcheck="false" placeholder="Search markets" aria-label="Search markets" role="combobox" aria-expanded="true" aria-controls="ex-ml">
                <button type="button" class="ex-mini2" id="ex-mclose" aria-label="Close the list">Close</button></div>
              <div class="ex-mh"><span>Market</span><span></span><span>Oracle price</span><span>Max leverage</span></div>
              <ul id="ex-ml" class="ex-ml" role="listbox" aria-label="Markets"></ul>
            </div>
          </div>
          <div class="ex-px"><b id="ex-px">—</b><span>Oracle price</span></div>
          <div class="ex-iv" id="ex-iv" role="group" aria-label="Interval"></div>
          <span class="ex-cap">${esc(SCREEN_TEXT.chart)}</span>
        </div>
        <div class="ex-cw" id="ex-chart"></div>
      </section>
      <section class="ex-pane ex-book"${ds.ok ? "" : " hidden"}>
        <div class="ex-ph"><b>Book</b><span class="ex-cap">${esc(SCREEN_TEXT.book)}</span></div>
        <div id="ex-book" class="ex-muted ex-pad">Loading…</div>
        <p class="ex-bn">${esc(SCREEN_TEXT.bookFill)}</p>
      </section>
      <div class="ex-right">
        <section class="ex-pane ex-form">
          <div class="ex-bs" role="group" aria-label="Side">
            <button type="button" data-side="buy" class="on b">Buy / Long</button>
            <button type="button" data-side="sell" class="s">Sell / Short</button>
          </div>
          <div class="ex-fl"><label for="ex-size">Size</label><span class="ex-inp"><input id="ex-size" inputmode="decimal" autocomplete="off" placeholder="0.00">
            <span class="ex-units" role="group" aria-label="Unit of the size"><button type="button" data-unit="asset" id="ex-u-asset"></button><button type="button" data-unit="usdc">USDC</button></span></span>
            <p class="ex-conv" id="ex-conv" aria-live="polite"></p></div>
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
    ${marks && !st.panelPool ? `<div class="ex-foot"><span class="ex-disc">${esc(marks.plaque)}</span></div>` : ""}`;
}

// ── readings ────────────────────────────────────────────────────

const attempt = (fn) => Promise.resolve().then(fn).catch(() => null);

/** What the page reads with: each getter of the exam, and the precompiles' raw answers (app/lib/examreads.js). */
function readerOf(st) {
  const { exam } = st;
  return {
    block: () => chain.readProvider.getBlock("latest"),
    account: () => Promise.all([exam.status(), exam.step(), exam.cash(), exam.dayStartEquity(), exam.tradingDays(), exam.deadline(), exam.lastFillAt(),
      exam.pending(), exam.filler(), exam.fromBook(), exam.maxWorseBps(), exam.drawdownKind(), exam.peakSnapshot(), exam.agent(),
      exam.rules(), exam.terms()]),
    position: (asset) => exam.positionOf(asset),
    stop: (asset) => exam.stopOf(asset),
    take: (asset) => exam.takeOf(asset),
    // With a stop book: a change asked of a stop or a take, and the book's touches of this exam not yet settled.
    ...(st.stopBook ? { change: (asset, isTake) => exam.changeOf(asset, isTake), touches: () => st.stopBook.touches(st.address) } : {}),
    oracle: (asset) => chain.readProvider.call(oraclePxCall(asset)),
    bbo: (asset) => chain.readProvider.call(bboCall(asset)),
  };
}

/** Everything the account needs, read from the chain at its latest block. A price that could not be read is null. */
async function readExam(st, selected) {
  const { block, got, perAsset, touches, fresh } = await readPass(readerOf(st), { assets: st.assets, selected, prices: st.prices });
  const [status, step, cash, dayStartEquity, tradingDays, deadline, lastFillAt, pending, filler, fromBook, maxWorseBps,
    drawdownKind, peakSnapshot, agent, rules, terms] = got;
  const reads = readsFrom({ status, step, cash, dayStartEquity, tradingDays, deadline, lastFillAt, pending, filler, fromBook,
    maxWorseBps, drawdownKind, peakSnapshot, rules, terms, stopBook: st.bookAddress }, perAsset, touches);
  const now = block.timestamp;
  const account = examAccount(reads, { now, block: block.number });
  return { reads, account, perAsset, fresh, agent, block: block.number, now, selected };
}

async function tick(page, st) {
  const pass = st.passes.start();
  try {
    const selected = st.asset;
    let snap;
    try {
      snap = await readExam(st, selected);
    } catch (err) {
      // The error of a pass that a newer one passed by is not told: the newer one says what the screen can say.
      if (!pass.isLatest()) return undefined;
      throw err;
    }
    // A pass that started after this one is out: only the latest one draws, so an older answer never covers a newer one.
    if (!pass.isLatest()) return undefined;
    // The market was changed while this was being read: what was read is not what the screen now needs. Read again.
    if (selected !== st.asset) return tick(page, st);
    st.snap = snap;
    // With the list closed only the prices this pass read are kept: the others would be old when the list opens again.
    st.prices = st.market.open ? withPrices(st.prices, snap.fresh) : snap.fresh;
    note(page, "");
    paintAccount(page, st);
    paintPositions(page, st);
    paintTicket(page, st);
    paintAgent(page, st);
    if (st.market.open) paintMarketList(page, st);
    trackEntry(st);
    return undefined;
  } finally {
    pass.end();
  }
}

/** The oracle prices of the markets a pass does not read, for the list while it is open. One reading at a time for each
 *  opening of the list: a reading that was out when the list was closed, or closed and opened again, is not kept. */
async function refreshList(page, st) {
  const session = st.listSession;
  if (!st.market.open || st.listReading === session) return;
  st.listReading = session;
  try {
    const fresh = await readList(readerOf(st), { assets: st.assets, skip: fullyRead(st.snap?.perAsset ?? []) });
    if (st.market.open && session === st.listSession) {
      // A pass that finished meanwhile read its markets later than the list did: its prices stand.
      st.prices = withPrices(st.prices, listPrices(fresh, fullyRead(st.snap?.perAsset ?? [])));
      if (page.isConnected) paintMarketList(page, st);
    }
  } finally {
    if (st.listReading === session) st.listReading = null;
  }
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

/** The market button and list, the unit of the size, and the address: it names the market, so a link opens the same one. */
function paintAssets(page, st) {
  const name = st.perps.get(st.asset)?.name ?? `#${st.asset}`;
  $("#ex-market-name", page).textContent = name;
  $("#ex-u-asset", page).textContent = name;
  page.querySelectorAll("[data-unit]").forEach((b) => {
    const on = b.dataset.unit === st.unit;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
  });
  // replaceState writes no entry and fires no hashchange: the page stays as it is, its link names the market.
  try {
    const base = location.hash.split("?")[0];
    history.replaceState(history.state, "", `${base}?${queryWithAsset(st.query, st.asset)}`);
  } catch {
    // the address is not writable here: the market is then the page's own
  }
  paintMarketList(page, st);
}

/** What the list shows of each market: its size and the latest oracle price read for it, by a pass or by the list's own reading. */
function listAssets(st) {
  return (st.snap?.perAsset ?? []).map((a) => ({ asset: a.asset, size: a.size, oracle: shownPrice(st.prices, a) }));
}

function paintMarketList(page, st) {
  const rows = marketRows({ assets: st.assets, perps: st.perps, perAsset: listAssets(st), rules: st.snap?.reads.rules, query: st.market.q });
  st.market.rows = rows;
  if (st.market.at >= rows.length) st.market.at = rows.length - 1;
  $("#ex-ml", page).innerHTML = rows.length ? rows.map((r, i) => `<li role="option" id="ex-mo-${r.asset}" data-asset="${r.asset}" aria-selected="${r.asset === st.asset}" class="${r.asset === st.asset ? "cur" : ""}${i === st.market.at ? " hl" : ""}">`
    + `<b>${esc(r.name)}</b>${r.held ? `<em title="A position is open on this asset">position</em>` : "<em></em>"}<span>${esc(r.price)}</span><span>${esc(r.leverage)}</span></li>`).join("")
    : `<li class="ex-none" role="presentation">No market of this exam matches.</li>`;
  const input = $("#ex-mq", page);
  if (st.market.at >= 0 && rows[st.market.at]) input.setAttribute("aria-activedescendant", `ex-mo-${rows[st.market.at].asset}`);
  else input.removeAttribute("aria-activedescendant");
}

function setMarketOpen(page, st, open, focus = true) {
  st.market.open = open;
  st.listSession += 1;
  $("#ex-pop", page).hidden = !open;
  $("#ex-market", page).setAttribute("aria-expanded", String(open));
  if (open) {
    st.market.q = "";
    $("#ex-mq", page).value = "";
    paintMarketList(page, st);
    st.market.at = Math.max(0, (st.market.rows ?? []).findIndex((r) => r.asset === st.asset));
    paintMarketList(page, st);
    $("#ex-mq", page).focus();
    $("#ex-ml", page).querySelector(".hl")?.scrollIntoView({ block: "nearest" });
    refreshList(page, st).catch(() => {});
  } else {
    // What the list was shown is not kept: a price of a market no pass reads would be old by the time the list opens again.
    const read = fullyRead(st.snap?.perAsset ?? []);
    st.prices = new Map([...st.prices].filter(([asset]) => read.has(asset) || asset === st.asset));
    if (focus) $("#ex-market", page).focus();
  }
}

function paintHead(page, st) {
  const perp = st.perps.get(st.asset);
  // A price kept as null is a price that was not read: no older one stands in for it.
  const oracle = shownPrice(st.prices, st.snap?.perAsset?.find((a) => a.asset === st.asset) ?? { asset: st.asset });
  const px = $("#ex-px", page);
  if (px) px.textContent = perp && oracle ? pxText(oracle, perp.szDecimals) : "—";
  st.chart?.setOracle(perp && oracle ? Number(pxText(oracle, perp.szDecimals)) : null);
  // The lines of this market's open position, from the account the page has read and the fills its journal holds.
  st.chart?.setPositionLines(positionLines({ account: st.snap?.account, asset: st.asset, perps: st.perps, entryOf: (asset, size) => st.entries?.entry(asset, size) ?? null }));
}

/** Tells the entry price's tracker the market on screen, its size and the exam's last fill and cash as the chain reports
 *  them: it reads the logs only when no proof stands for that state (never on a timer). Nothing is waited for. */
function trackEntry(st) {
  const p = st.snap?.account.positions.find((x) => x.asset === st.asset);
  // The exam's lastFillAt and cash are read by the screen's own reading already: a fill of any market moves them.
  st.entries?.observe({ asset: st.asset, size: p ? p.size : 0n, block: st.snap.block, fillAt: st.snap.reads.lastFillAt, cash: st.snap.reads.cash });
}

/** The exam's fills and stops between two blocks, decoded, the way one press of the journal's button reads them: windows of
 *  `JOURNAL_WINDOW` blocks, at most `JOURNAL_CALLS` calls (1000 blocks, as the tracker's `READ_BLOCKS`). A reading that did not reach its end fails. */
async function readEntryEvents(st, from, to) {
  const { logs, complete } = await readLogs({ from, to, window: JOURNAL_WINDOW, maxCalls: JOURNAL_CALLS,
    getLogs: (x, y) => chain.readProvider.getLogs({ address: st.address, fromBlock: x, toBlock: y }) });
  if (!complete) throw new Error("the logs were not read to the end");
  return logs.map((l) => {
    const parsed = st.exam.interface.parseLog(l);
    return parsed ? { name: parsed.name, args: parsed.args, block: l.blockNumber, index: l.index } : null;
  }).filter(Boolean).sort((p, q) => p.block - q.block || p.index - q.index);
}

/** The asset the chart draws: its Hyperliquid name and size decimals. */
function chartAsset(st) {
  const perp = st.perps.get(st.asset);
  return { coin: perp.name, szDecimals: perp.szDecimals };
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
        `<span>${esc(t)} <button type="button" class="ex-x" data-clear="${r.asset}:${k}"${st.flight.busy ? " disabled" : ""} aria-label="Remove ${k}" title="Remove ${k}">×</button></span>`).join("") || "—"}${
        r.touched && st.bookAddress ? ` <button type="button" class="ex-cl" data-settle="1"${st.flight.busy ? " disabled" : ""} title="${esc(SCREEN_TEXT.settle)}">Settle</button>` : ""}</td>
      <td data-l=""><button type="button" class="ex-cl" data-close="${r.asset}"${st.flight.busy ? " disabled" : ""}>Close</button></td></tr>`).join("")}</tbody></table>`
    : `<p class="ex-pad ex-muted">No open positions.</p>`) + `<p class="ex-exit">${esc(SCREEN_TEXT.exit)}</p>`
    + (st.bookAddress ? `<p class="ex-exit">${esc(SCREEN_TEXT.stopRule)}</p>` : "");
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
  if (!prices || !prices.read) {
    // This market was chosen after the last pass: its stop, take and prices arrive with the next reading, a moment.
    $("#ex-preview", page).innerHTML = `<p class="ex-note">Reading this market…</p>`;
    $("#ex-conv", page).textContent = "";
    send.disabled = true;
    for (const kind of ["stop", "take"]) {
      $(`#ex-${kind}-prev`, page).innerHTML = "";
      $(`#ex-${kind}-set`, page).disabled = true;
    }
    st.order = null;
    st.triggers = {};
    return;
  }
  const priced = { oracle: prices.oracle, bid: prices.bid, ask: prices.ask };
  const entry = sizeEntry(reads, { perp, isBuy: st.isBuy, unit: st.unit, input: $("#ex-size", page).value, prices: priced });
  const field = $("#ex-size", page);
  field.disabled = Boolean(entry.disabled);
  field.title = entry.disabled ?? "";
  field.placeholder = st.unit === "usdc" ? "0.00 USDC" : "0.00";
  $("#ex-conv", page).textContent = entry.disabled ?? (entry.conv || "");
  const form = entry.error ? { size: null, error: entry.error, preview: null }
    : orderForm(reads, { asset: st.asset, perp, isBuy: st.isBuy, sizeInput: entry.sizeInput, prices: priced, now, block });
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
      // A settle goes with its fixed gas (`SETTLE_GAS`): an estimate would find the gas of doing nothing.
      const tx = await signer.sendTransaction({ to: call.to, data: call.data, ...(call.gas ? { gasLimit: call.gas } : {}) });
      el.textContent = "Sent. Waiting for the block…";
      const receipt = await tx.wait();
      chain.forgetLiveReads();
      if (!receipt || receipt.status !== 1) throw new Error(`${call.method} reverted`);
      el.className = "result ok";
      el.textContent = call.method === "place" ? "Order sent. It is filled a few blocks later."
        : call.method === "settle" ? SCREEN_TEXT.settled : "Done.";
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
    st.book = null;
    $("#ex-book", page).textContent = "Loading…";
    paintAssets(page, st);
    st.chart?.setAsset(chartAsset(st));
    refreshBook(page, st);
    trackEntry(st);
    choose();
    // The new market's stop, take, prices and book are not in the last pass: read them now.
    tick(page, st).catch((err) => note(page, `Could not refresh: ${friendly(err)}`));
  };
  const on = (list, picked) => list.forEach((x) => x.classList.toggle("on", x === picked));
  const pick = (asset) => {
    if (asset !== st.asset) setAsset(asset);
    setMarketOpen(page, st, false);
  };
  page.addEventListener("click", (e) => {
    const li = e.target.closest("#ex-ml li[data-asset]");
    if (li) {
      pick(Number(li.dataset.asset));
      return;
    }
    // a click elsewhere closes the list, and the click goes on to what it was on
    if (st.market.open && !e.target.closest(".ex-mk")) setMarketOpen(page, st, false, false);
  });
  page.addEventListener("input", (e) => {
    if (e.target.id !== "ex-mq") return;
    st.market.q = e.target.value;
    st.market.at = 0;
    paintMarketList(page, st);
  });
  page.addEventListener("keydown", (e) => {
    const rows = st.market.rows ?? [];
    if (e.target.id === "ex-market" && (e.key === "ArrowDown" || e.key === "ArrowUp") && !st.market.open) {
      e.preventDefault();
      setMarketOpen(page, st, true);
    } else if (e.key === "Escape" && st.market.open) {
      e.preventDefault();
      setMarketOpen(page, st, false);
    } else if (e.target.id === "ex-mq") {
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
        e.preventDefault();
        st.market.at = stepIndex(rows.length, st.market.at, e.key);
        paintMarketList(page, st);
        $("#ex-ml", page).querySelector(".hl")?.scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && rows[st.market.at]) {
        e.preventDefault();
        pick(rows[st.market.at].asset);
      }
    }
  });
  page.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || !page.contains(b)) return;
    if (b.id === "ex-market") {
      setMarketOpen(page, st, !st.market.open);
    } else if (b.id === "ex-mclose") {
      setMarketOpen(page, st, false);
    } else if (b.dataset.unit) {
      if (b.dataset.unit === st.unit) return;
      st.unit = b.dataset.unit;
      store(UNIT_KEY, st.unit);
      $("#ex-size", page).value = "";
      paintAssets(page, st);
      choose();
    } else if (b.dataset.phone) {
      $("#ex-term", page).dataset.phone = b.dataset.phone;
      on([...page.querySelectorAll("[data-phone]")].filter((x) => x.tagName === "BUTTON"), b);
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
    } else if (b.dataset.settle) {
      if (!st.bookAddress) return;
      sendCall(page, st, settleCall({ book: st.bookAddress, exam: st.address, by: agentActive(st) ? "agent" : "trader", keccak256: keccak }), "#ex-send-out");
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
    info.textContent = journalRange(st.loaded) + (canLoadEarlier(st.loaded) ? "" : " That is the start of the chain.");
    paintJournal(page, st);
    // What the button read may prove the entry price too.
    const held = st.snap.account.positions.find((x) => x.asset === st.asset);
    if (held) st.entries?.feed({ events: st.journal, from: st.loaded.from, to: st.loaded.top }, { asset: st.asset, size: held.size, block: st.snap.block, fillAt: st.snap.reads.lastFillAt, cash: st.snap.reads.cash });
    buttons[0].textContent = "Load earlier events";
    buttons[1].hidden = false;
  } catch (err) {
    info.textContent = `Could not load: ${friendly(err)}`;
  } finally {
    buttons.forEach((x) => { x.disabled = false; });
    // The range has reached block 0: there is nothing earlier to load.
    buttons[0].disabled = !canLoadEarlier(st.loaded);
  }
}
