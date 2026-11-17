// The chart of the trading page, behind a narrow door: TradingView Lightweight Charts (app/vendor/lightweight-charts/,
// Apache-2.0, unmodified) drawing Hyperliquid's candles. Another chart can stand in its place by keeping this file's
// one export and its five methods; nothing else in the page knows which library draws.
//
//   const chart = createExamChart({ node, bar, load, explain });
//   chart.setAsset({ coin: "BTC", szDecimals: 5 });   // the asset whose candles are drawn
//   chart.setInterval("4h");                           // one of INTERVALS (app/lib/chartdata.js)
//   chart.setOracle(91250);                            // the line of the oracle's price; null takes it away
//   chart.destroy();                                   // the page was left
//
// `node` holds the chart and what is written under it; `bar` is where the interval buttons go; `load(coin, interval,
// startTime, endTime, { signal })` is Hyperliquid's candle request (hl.candles) and answers its raw reply. Nothing is drawn that
// Hyperliquid did not send: when no candles are read the chart is empty and says why.
import { candleRows } from "../lib/exam.js";
import { singleFlight } from "../lib/examscreen.js";
import {
  BAR_INTERVALS, MORE_INTERVALS, DEFAULT_INTERVAL, CANDLES_SHOWN, CANDLES_MORE, UP, DOWN, isInterval,
  candleWindow, olderWindow, once, pollStep, withTimeout, REQUEST_MS, afterOlder, historyStart, mergeRows, tailRows, toSeries, priceDecimals, candleText,
} from "../lib/chartdata.js";

const LIBRARY = new URL("../vendor/lightweight-charts/lightweight-charts.standalone.production.js", import.meta.url).href;
const STORE_KEY = "exam-chart-interval";
const POLL_MS = 20000;
const RETRY_MS = 8000;
/** The candles in view when a series is first drawn. */
const IN_VIEW = 120;
/** Older candles are asked for when the view comes this close to the left edge of what is drawn. */
const EDGE = 15;
export const ATTRIBUTION = "TradingView Lightweight Charts™ Copyright (c) 2026 TradingView, Inc.";
const TRADINGVIEW = "https://www.tradingview.com/";

function keep(value) {
  try {
    if (value === undefined) return localStorage.getItem(STORE_KEY);
    localStorage.setItem(STORE_KEY, value);
  } catch { /* a browser without storage forgets the interval, nothing more */ }
  return null;
}

let libraryAsked = null;
/** The library, loaded once from this site's own copy: a failure is not kept, so the next asking tries again. */
function library() {
  if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
  if (!libraryAsked) {
    libraryAsked = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = LIBRARY;
      s.onload = () => (window.LightweightCharts ? resolve(window.LightweightCharts) : reject(new Error("the chart library is empty")));
      s.onerror = () => reject(new Error("the chart library did not load"));
      document.head.appendChild(s);
    }).catch((err) => {
      libraryAsked = null;
      throw err;
    });
  }
  return libraryAsked;
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

export function createExamChart({ node, bar, load, explain = (e) => (e && e.message) || String(e), now = Date.now }) {
  const host = el("div", "ex-cv");
  const empty = el("p", "ex-empty", "Loading candles…");
  const legend = el("p", "ex-hover");
  legend.innerHTML = "&nbsp;";
  const credit = el("p", "ex-attr");
  credit.append(`${ATTRIBUTION} `);
  const link = el("a", "", TRADINGVIEW);
  link.href = TRADINGVIEW;
  link.target = "_blank";
  link.rel = "noopener";
  credit.append(link);
  host.append(empty);
  node.replaceChildren(host, legend, credit);

  const state = {
    coin: null, szDecimals: 0, interval: isInterval(keep()) ? keep() : DEFAULT_INTERVAL, rows: [], byTime: new Map(),
    hist: { beforeMs: Infinity, misses: 0, done: true }, run: 0, older: false, retryAt: 0, oracle: null, priceLine: null,
    chart: null, candles: null, volume: null, lib: null, dead: false, timer: null, loading: false, tail: singleFlight(),
  };
  // Every candle request has a limit on its wait, so a request that never answers is a failed reading, not a stuck one.
  const ask = (...args) => withTimeout((signal) => load(...args, { signal }), REQUEST_MS);
  // The library is loaded and the chart built once, however many first readings ask while the script is on its way.
  const ready = once(async () => {
    const lib = await library();
    if (!state.dead && !state.chart) build(lib);
  });

  // ── the interval buttons ─────────────────────────────────────
  const more = el("select", "ex-more");
  more.setAttribute("aria-label", "More intervals");
  more.append(new Option("More", ""));
  for (const key of MORE_INTERVALS) more.append(new Option(key, key));
  const buttons = BAR_INTERVALS.map((key) => {
    const b = el("button", "", key);
    b.type = "button";
    b.dataset.interval = key;
    return b;
  });
  bar.replaceChildren(...buttons, more);
  const paintBar = () => {
    for (const b of buttons) b.classList.toggle("on", b.dataset.interval === state.interval);
    const inMore = MORE_INTERVALS.includes(state.interval);
    more.value = inMore ? state.interval : "";
    more.classList.toggle("on", inMore);
  };
  bar.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-interval]");
    if (b && bar.contains(b)) api.setInterval(b.dataset.interval);
  });
  more.addEventListener("change", () => { if (more.value) api.setInterval(more.value); else paintBar(); });
  paintBar();

  // ── what is shown when there is nothing to draw ──────────────
  const say = (text) => {
    empty.textContent = text || "";
    empty.hidden = !text;
  };
  const tell = (text) => {
    legend.textContent = text || " ";
  };
  const lastText = () => candleText(state.rows[state.rows.length - 1]) || " ";

  // ── the chart ────────────────────────────────────────────────
  function colors() {
    const css = getComputedStyle(node);
    const get = (n, fallback) => css.getPropertyValue(n).trim() || fallback;
    return { paper: get("--paper", "#F5F5F2"), text: get("--mute", "#5E6670"), grid: get("--line", "#D6D8D6"),
      ink: get("--ink", "#0E1116"), up: get("--up", UP), down: get("--dn", DOWN), mono: get("--mono", "monospace") };
  }

  function build(lib) {
    const c = colors();
    const chart = lib.createChart(host, {
      autoSize: true,
      layout: { background: { type: "solid", color: c.paper }, textColor: c.text, fontFamily: c.mono, fontSize: 11, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.grid, scaleMargins: { top: 0.06, bottom: 0.2 } },
      timeScale: { borderColor: c.grid, timeVisible: true, secondsVisible: false, rightOffset: 6, minBarSpacing: 1 },
      crosshair: { mode: lib.CrosshairMode.Normal },
      // Times are UTC (the library's own); the month names are English, whatever the browser's language.
      localization: { locale: "en-GB" },
      // A finger moving up or down scrolls the page, not the chart; sideways it scrolls the chart.
      handleScroll: { vertTouchDrag: false },
    });
    const candles = chart.addSeries(lib.CandlestickSeries, {
      upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down, wickUpColor: c.up, wickDownColor: c.down,
      priceLineStyle: lib.LineStyle.Dashed, priceLineColor: c.ink, priceLineWidth: 1,
    });
    const volume = chart.addSeries(lib.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.subscribeCrosshairMove((p) => {
      const row = p.time !== undefined ? state.byTime.get(p.time) : null;
      tell(row ? candleText(row) : lastText());
    });
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range && range.from < EDGE) loadOlder();
    });
    state.chart = chart;
    state.candles = candles;
    state.volume = volume;
    state.lib = lib;
    state.colors = c;
    paintOracle();
  }

  /** The price scale is written only beside candles: an empty chart has no prices to name, least of all another asset's. */
  function showScale(visible) {
    if (state.chart) state.chart.priceScale("right").applyOptions({ visible });
  }

  function format() {
    if (!state.candles || !state.rows.length) return;
    const decimals = priceDecimals(state.rows[state.rows.length - 1].close, state.szDecimals);
    state.candles.applyOptions({ priceFormat: { type: "price", precision: decimals, minMove: 10 ** -decimals } });
  }

  function paintOracle() {
    if (!state.candles) return;
    if (state.priceLine) {
      state.candles.removePriceLine(state.priceLine);
      state.priceLine = null;
    }
    const has = state.oracle !== null;
    // The oracle's price is the one an order fills at: when it is known its line stands for the last price, and
    // the library's own line (the last candle's close) steps aside.
    state.candles.applyOptions({ priceLineVisible: !has, lastValueVisible: !has });
    if (has) {
      state.priceLine = state.candles.createPriceLine({ price: state.oracle, color: state.colors.ink, lineWidth: 1,
        lineStyle: state.lib.LineStyle.Dashed, axisLabelVisible: true, title: "" });
    }
  }

  function draw(rows) {
    state.rows = rows;
    state.byTime = new Map(rows.map((r) => [r.time, r]));
    const s = toSeries(rows, state.colors);
    state.candles.setData(s.candles);
    state.volume.setData(s.volume);
  }

  /** Draws `rows` with the chart looking at the same candles as before: stretches added on the left shift the indices. */
  function redrawKeepingView(rows) {
    const ts = state.chart.timeScale();
    const view = ts.getVisibleLogicalRange();
    const before = state.rows.length;
    draw(rows);
    if (view) {
      const added = rows.length - before;
      ts.setVisibleLogicalRange({ from: view.from + added, to: view.to + added });
    }
  }

  // ── reading Hyperliquid ──────────────────────────────────────
  async function first() {
    if (state.dead || !state.coin) return;
    const run = ++state.run;
    state.older = false;
    state.rows = [];
    state.byTime = new Map();
    state.hist = { beforeMs: Infinity, misses: 0, done: true };
    state.loading = true;
    say("Loading candles…");
    tell("");
    try {
      await ready();
      if (state.dead || run !== state.run || !state.chart) return;
      state.candles.setData([]);
      state.volume.setData([]);
      showScale(false);
      const { startTime, endTime } = candleWindow(state.interval, now(), CANDLES_SHOWN);
      const rows = candleRows(await ask(state.coin, state.interval, startTime, endTime));
      if (state.dead || run !== state.run) return;
      if (!rows.length) {
        say(`Hyperliquid sent no ${state.interval} candles for ${state.coin}.`);
        return;
      }
      say("");
      draw(rows);
      showScale(true);
      format();
      state.hist = historyStart(rows);
      state.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, rows.length - IN_VIEW), to: rows.length + 6 });
      tell(lastText());
    } catch (err) {
      if (state.dead || run !== state.run) return;
      say(`Candles not read: ${explain(err)}`);
    } finally {
      if (run === state.run) state.loading = false;
    }
  }

  async function loadOlder() {
    if (state.dead || state.older || state.hist.done || !state.rows.length || now() < state.retryAt) return;
    const run = state.run;
    state.older = true;
    try {
      while (!state.hist.done) {
        const w = olderWindow(state.interval, state.hist.beforeMs, now(), CANDLES_MORE);
        if (!w) {
          state.hist = { ...state.hist, done: true };
          break;
        }
        const got = candleRows(await ask(state.coin, state.interval, w.startTime, w.endTime));
        if (state.dead || run !== state.run) return;
        state.hist = afterOlder(state.hist, w, got);
        if (got.length) {
          redrawKeepingView(mergeRows(state.rows, got));
          break;
        }
      }
    } catch (err) {
      if (state.dead || run !== state.run) return;
      state.retryAt = now() + RETRY_MS;
      tell(`Older candles not read: ${explain(err)}`);
    } finally {
      if (run === state.run) state.older = false;
    }
  }

  async function poll() {
    if (state.dead || !state.coin) return;
    const step = pollStep({ rows: state.rows.length > 0, loading: state.loading, older: state.older, polling: state.tail.busy,
      hidden: typeof document !== "undefined" && document.hidden });
    if (step === "skip") return;
    if (step === "first") {
      first();
      return;
    }
    // One reading of the tail at a time: a slow answer is not overtaken by a newer one and then written over it.
    await state.tail.run(async () => {
      const run = state.run;
      const last = state.rows[state.rows.length - 1].time;
      try {
        const got = candleRows(await ask(state.coin, state.interval, last * 1000, now()));
        if (state.dead || run !== state.run) return;
        const tail = tailRows(state.rows, got);
        if (!tail.length) return;
        const s = toSeries(tail, state.colors);
        s.candles.forEach((c) => state.candles.update(c));
        s.volume.forEach((v) => state.volume.update(v));
        state.rows = mergeRows(state.rows, tail);
        for (const r of tail) state.byTime.set(r.time, r);
        format();
      } catch {
        // The candles on the screen stay as they are; the next reading tries again.
      }
    });
  }
  state.timer = setInterval(poll, POLL_MS);

  const api = {
    setAsset({ coin, szDecimals = 0 }) {
      if (coin === state.coin && szDecimals === state.szDecimals) return;
      state.coin = coin;
      state.szDecimals = szDecimals;
      state.oracle = null;
      paintOracle();
      first();
    },
    setInterval(key) {
      if (!isInterval(key)) throw new Error(`${key} is not an interval of the chart`);
      if (key === state.interval) return;
      state.interval = key;
      keep(key);
      paintBar();
      first();
    },
    setOracle(price) {
      state.oracle = Number.isFinite(price) && price > 0 ? price : null;
      paintOracle();
    },
    destroy() {
      state.dead = true;
      clearInterval(state.timer);
      if (state.chart) state.chart.remove();
      state.chart = null;
      node.replaceChildren();
      bar.replaceChildren();
    },
    get interval() { return state.interval; },
  };
  return api;
}

