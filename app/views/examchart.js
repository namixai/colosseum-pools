// The chart of the trading page, behind a narrow door: TradingView Lightweight Charts (app/vendor/lightweight-charts/,
// Apache-2.0, unmodified) drawing Hyperliquid's candles. Another chart can stand in its place by keeping this file's
// one export and its five methods; nothing else in the page knows which library draws.
//
//   const chart = createExamChart({ node, bar, load, explain });
//   chart.setAsset({ coin: "BTC", szDecimals: 5 });   // the asset whose candles are drawn
//   chart.setInterval("4h");                           // one of INTERVALS (app/lib/chartdata.js)
//   chart.setOracle(91250);                            // the line of the oracle's price; null takes it away
//   chart.setPositionLines({ entry, stop, take });     // the open position's lines (each a price or null); null takes them away
//   chart.destroy();                                   // the page was left
//
// `node` holds the chart and what is written under it; `bar` is where the interval buttons go; `load(coin, interval,
// startTime, endTime, { signal })` is Hyperliquid's candle request (hl.candles) and answers its raw reply. Nothing is drawn that
// Hyperliquid did not send: when no candles are read the chart is empty and says why. The "Indicators" button in the
// bar switches the volume under the candles and three moving averages (app/lib/indicators.js) on and off.
import { candleRows } from "../lib/exam.js";
import { singleFlight } from "../lib/examscreen.js";
import { emaPoints, INDICATORS, readChoice, choiceText } from "../lib/indicators.js";
import {
  BAR_INTERVALS, MORE_INTERVALS, DEFAULT_INTERVAL, CANDLES_SHOWN, CANDLES_MORE, UP, DOWN, isInterval,
  candleWindow, olderWindow, once, pollStep, withTimeout, REQUEST_MS, afterOlder, historyStart, mergeRows, tailRows, toSeries, priceDecimals, candleText,
} from "../lib/chartdata.js";

const LIBRARY = new URL("../vendor/lightweight-charts/lightweight-charts.standalone.production.js", import.meta.url).href;
const STORE_KEY = "exam-chart-interval";
const CHOICE_KEY = "exam-chart-indicators";
const POLL_MS = 20000;
const RETRY_MS = 8000;
/** The candles in view when a series is first drawn. */
const IN_VIEW = 120;
/** Older candles are asked for when the view comes this close to the left edge of what is drawn. */
const EDGE = 15;
/** A line of the position this near the price (a share of it) is kept in view by the price scale; a farther one is not. */
const LINE_REACH = 0.15;
export const ATTRIBUTION = "TradingView Lightweight Charts™ Copyright (c) 2026 TradingView, Inc.";
const TRADINGVIEW = "https://www.tradingview.com/";

function keep(value) {
  try {
    if (value === undefined) return localStorage.getItem(STORE_KEY);
    localStorage.setItem(STORE_KEY, value);
  } catch { /* a browser without storage forgets the interval, nothing more */ }
  return null;
}

function keepChoice(text) {
  try {
    if (text === undefined) return localStorage.getItem(CHOICE_KEY);
    localStorage.setItem(CHOICE_KEY, text);
  } catch { /* a browser without storage goes back to the default choice on the next visit, nothing more */ }
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
  const emaLegend = el("div", "ex-leg");
  emaLegend.hidden = true;
  host.append(empty, emaLegend);
  node.replaceChildren(host, legend, credit);

  const state = {
    coin: null, szDecimals: 0, interval: isInterval(keep()) ? keep() : DEFAULT_INTERVAL, rows: [], byTime: new Map(),
    hist: { beforeMs: Infinity, misses: 0, done: true }, run: 0, older: false, retryAt: 0, oracle: null, priceLine: null,
    chart: null, candles: null, volume: null, lib: null, dead: false, timer: null, loading: false, tail: singleFlight(),
    choice: readChoice(keepChoice()), emas: new Map(), decimals: 2, lines: null, lineSeries: [], lineKey: "",
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

  // ── the Indicators button: one list of four switches ─────────
  const menuId = `ex-ind-${Math.random().toString(36).slice(2, 8)}`;
  const indWrap = el("span", "ex-indw");
  const indBtn = el("button", "ex-indbtn", "Indicators");
  indBtn.type = "button";
  indBtn.setAttribute("aria-haspopup", "true");
  indBtn.setAttribute("aria-expanded", "false");
  indBtn.setAttribute("aria-controls", menuId);
  const indMenu = el("div", "ex-indmenu");
  indMenu.id = menuId;
  indMenu.hidden = true;
  indMenu.setAttribute("role", "group");
  indMenu.setAttribute("aria-label", "Indicators");
  const boxes = INDICATORS.map((def) => {
    const label = el("label", "");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.indicator = def.key;
    const mark = el("i", "ex-sw");
    if (def.color) mark.style.background = def.color;
    else mark.hidden = true;
    label.append(box, mark, def.label);
    indMenu.append(label);
    return box;
  });
  indWrap.append(indBtn, indMenu);
  bar.append(indWrap);
  const paintChoice = () => {
    for (const box of boxes) box.checked = Boolean(state.choice[box.dataset.indicator]);
  };
  /** The list opens under the button and is kept inside the screen: on a narrow one the button may sit at either edge. */
  const openMenu = (open) => {
    indMenu.hidden = !open;
    indBtn.setAttribute("aria-expanded", String(open));
    if (!open) return;
    indMenu.style.left = "0";
    indMenu.style.right = "auto";
    const r = indMenu.getBoundingClientRect();
    const room = document.documentElement.clientWidth - 8;
    if (r.right > room) {
      indMenu.style.left = "auto";
      indMenu.style.right = "0";
    }
  };
  indBtn.addEventListener("click", () => openMenu(indMenu.hidden));
  indMenu.addEventListener("change", (e) => {
    const box = e.target.closest("input[data-indicator]");
    if (box) api.setIndicator(box.dataset.indicator, box.checked);
  });
  const outside = (e) => { if (!indMenu.hidden && !indWrap.contains(e.target)) openMenu(false); };
  const escape = (e) => {
    if (e.key === "Escape" && !indMenu.hidden) {
      openMenu(false);
      indBtn.focus();
    }
  };
  document.addEventListener("click", outside);
  document.addEventListener("keydown", escape);
  paintChoice();

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
      ink: get("--ink", "#0E1116"), navy: get("--navy", "#1F3550"), up: get("--up", UP), down: get("--dn", DOWN), mono: get("--mono", "monospace") };
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
      // The position's lines stay in view when they are near the price: a stop a few per cent away is on the chart.
      autoscaleInfoProvider: (original) => {
        const r = original();
        const last = state.rows.length ? state.rows[state.rows.length - 1].close : null;
        const near = last === null || !r ? [] : linePrices().filter((p) => Math.abs(p - last) / last <= LINE_REACH);
        if (!near.length) return r;
        return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, ...near), maxValue: Math.max(r.priceRange.maxValue, ...near) } };
      },
    });
    // The volume has a scale of its own along the bottom, so its bars never cover a candle.
    const volume = chart.addSeries(lib.HistogramSeries, { priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false,
      priceLineVisible: false, visible: state.choice.volume });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.subscribeCrosshairMove((p) => {
      const row = p.time !== undefined ? state.byTime.get(p.time) : null;
      tell(row ? candleText(row) : lastText());
      paintLegend(p.time);
    });
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range && range.from < EDGE) loadOlder();
    });
    state.chart = chart;
    state.candles = candles;
    state.volume = volume;
    state.lib = lib;
    state.colors = c;
    paintVolume();
    for (const def of INDICATORS) if (def.period && state.choice[def.key]) addEma(def);
    paintOracle();
    paintLines();
  }

  // ── the volume and the moving averages ───────────────────────
  /** The volume shows or hides with its switch; the candles take the room it leaves when it is off. */
  function paintVolume() {
    if (!state.volume) return;
    state.volume.applyOptions({ visible: state.choice.volume });
    state.chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: state.choice.volume ? 0.2 : 0.06 } });
  }

  const fixed = (v) => (v === null || v === undefined ? "—" : v.toFixed(state.decimals));

  /** The legend of the averages that are on, each with its value at `time` (the last one without a time under the pointer). */
  function paintLegend(time) {
    const on = INDICATORS.filter((d) => d.period && state.choice[d.key] && state.emas.has(d.key));
    emaLegend.hidden = !on.length || !state.rows.length;
    emaLegend.replaceChildren(...on.map((def) => {
      const e = state.emas.get(def.key);
      const item = el("span", "");
      const mark = el("i", "ex-sw");
      mark.style.background = def.color;
      const value = time !== undefined && e.vals.has(time) ? e.vals.get(time) : time !== undefined ? null : e.last;
      item.append(mark, `${def.label} `, el("b", "", fixed(value)));
      if (value === null && !e.vals.size) item.title = `Fewer than ${def.period} candles: the average does not start yet`;
      return item;
    }));
  }

  /** The line of an average, drawn from the candles that are on the chart. */
  function addEma(def) {
    const series = state.chart.addSeries(state.lib.LineSeries, { color: def.color, lineWidth: 1, lastValueVisible: false, priceLineVisible: false,
      crosshairMarkerVisible: false });
    const e = { series, vals: new Map(), last: null };
    state.emas.set(def.key, e);
    fillEma(def, e);
  }

  function fillEma(def, e) {
    const points = emaPoints(state.rows, def.period);
    e.series.setData(points);
    e.vals = new Map(points.map((p) => [p.time, p.value]));
    e.last = points.length ? points[points.length - 1].value : null;
  }

  /** The averages of what is drawn now: every line is set again (older candles added on the left move where each starts). */
  function paintEmas() {
    for (const def of INDICATORS) {
      const e = state.emas.get(def.key);
      if (e) fillEma(def, e);
    }
    paintLegend();
  }

  /** New candles at the end: only the averages from the first of them on are given to the chart. */
  function updateEmas(fromTime) {
    for (const def of INDICATORS) {
      const e = state.emas.get(def.key);
      if (!e) continue;
      const points = emaPoints(state.rows, def.period);
      for (const p of points) {
        if (p.time < fromTime) continue;
        e.series.update(p);
        e.vals.set(p.time, p.value);
      }
      e.last = points.length ? points[points.length - 1].value : null;
    }
    paintLegend();
  }

  // ── the position's lines ─────────────────────────────────────
  const LINES = [["entry", "Entry", "Solid"], ["stop", "Stop", "SparseDotted"], ["take", "Take", "SparseDotted"]];
  const lineColor = (kind, c) => ({ entry: c.navy, stop: "#B3261E", take: "#1E8E5A" })[kind];

  const linePrices = () => LINES.map(([k]) => state.lines?.[k]).filter((p) => Number.isFinite(p) && p > 0);

  /** Draws the lines in `state.lines`, each at its price with its name; only when what is to be drawn has changed. */
  function paintLines() {
    if (!state.candles) return;
    const wanted = LINES.filter(([key]) => Number.isFinite(state.lines?.[key]) && state.lines[key] > 0);
    const key = JSON.stringify(wanted.map(([k]) => [k, state.lines[k]]));
    if (key === state.lineKey) return;
    for (const l of state.lineSeries) state.candles.removePriceLine(l);
    state.lineSeries = wanted.map(([k, title, style]) => state.candles.createPriceLine({ price: state.lines[k], color: lineColor(k, state.colors),
      lineWidth: 1, lineStyle: state.lib.LineStyle[style], axisLabelVisible: true, title }));
    state.lineKey = key;
    state.candles.applyOptions({});  // the scale is worked out again with the lines in it
  }

  /** The price scale is written only beside candles: an empty chart has no prices to name, least of all another asset's. */
  function showScale(visible) {
    if (state.chart) state.chart.priceScale("right").applyOptions({ visible });
  }

  function format() {
    if (!state.candles || !state.rows.length) return;
    const decimals = priceDecimals(state.rows[state.rows.length - 1].close, state.szDecimals);
    state.decimals = decimals;
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
    paintEmas();
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
      for (const e of state.emas.values()) {
        e.series.setData([]);
        e.vals = new Map();
        e.last = null;
      }
      paintLegend();
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
      paintLegend();
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
        updateEmas(tail[0].time);
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
      state.lines = null;
      paintLines();
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
    /** The lines of the open position of the market on screen: {entry, stop, take} (each a price or null); null for none. */
    setPositionLines(lines) {
      state.lines = lines;
      paintLines();
    },
    setIndicator(key, on) {
      const def = INDICATORS.find((d) => d.key === key);
      if (!def) throw new Error(`${key} is not an indicator of the chart`);
      state.choice = { ...state.choice, [key]: Boolean(on) };
      keepChoice(choiceText(state.choice));
      paintChoice();
      if (state.chart) {
        if (key === "volume") paintVolume();
        else if (on && !state.emas.has(key)) addEma(def);
        else if (!on && state.emas.has(key)) {
          state.chart.removeSeries(state.emas.get(key).series);
          state.emas.delete(key);
        }
        paintLegend();
      }
    },
    destroy() {
      state.dead = true;
      clearInterval(state.timer);
      document.removeEventListener("click", outside);
      document.removeEventListener("keydown", escape);
      if (state.chart) state.chart.remove();
      state.chart = null;
      node.replaceChildren();
      bar.replaceChildren();
    },
    get interval() { return state.interval; },
    get indicators() { return { ...state.choice }; },
  };
  return api;
}

