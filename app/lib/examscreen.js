// What the paper-exam trading page (app/views/exam.js) works out that is not drawing: the reads it hands to
// app/lib/exam.js, the words beside each number, the form's input turned into an order, the scale of the account, the
// journal's rows and the book's rows. No browser globals and no reads here, so node's test runner loads it
// (app/tests/examscreen.test.mjs). Every sum of money is app/lib/exam.js's: this file adds nothing up, it only says
// what that file answered, and where it places a number on a bar it works from the numbers that file gave.
import {
  ESTIMATE_NOTE, WHAT_THIS_IS, sizeUnits, pxUnits, sizeText, pxText, usdText, orderPreview, triggerSetPreview, refusalText,
  bookSides, sizeFromUsdc, fillPrice,
} from "./exam.js";

export { WHAT_THIS_IS, ESTIMATE_NOTE };

/** The lines the page says in plain sight, beside the strip at the top (the brief for this page). */
export const SCREEN_TEXT = Object.freeze({
  strip: "Exam",
  testnetPlaque: "Testnet. Mock USDC. Unaudited.",
  testnetTag: "Testnet app",
  usdcNoPrice: "The price of this asset was not read, so an amount in USDC is off.",
  fillsLater: "Orders fill a few seconds after you send them, at the price of that moment.",
  exit: "To exit a position, send an order yourself, or set a stop or a take on it.",
  gas: "Each order is a transaction: a signature and gas. Or name a second key and trade without prompts.",
  chart: "Chart: Hyperliquid prices",
  book: "Book: Hyperliquid",
  bookFill: "Your order fills at the best price of your side, never better than the oracle price. Size does not move the price.",
  secondKey: "A second key can place orders and pass the step on this exam. It cannot move money: an exam holds none. "
    + "It is kept in this browser; anyone who gets it can trade this exam. It needs a little HYPE for gas.",
  triggerReached: "The oracle price is already past this level: it will close the position as soon as it acts.",
  breach: "This order would put the account under its loss limit.",
  noPrice: "Price not read",
  oneAtATime: "one order at a time: a new one is accepted once this one is filled or lapsed",
  noAsset: "This asset's data was not read from Hyperliquid, so no size or price can be taken for it.",
  closeDrops: "An order that closes or turns a position round takes its stop and take off.",
  // The stop book's rules, as the contract carries them out (src/sim/StopBook.sol, docs/SIM-POOL.md).
  stopRule: "A stop or a take is filled at the oracle price of the first block that touches its level, from the block it "
    + "acts from. Taking one off or moving it acts after the same delay; until then the one there stands.",
  settle: "The oracle price touched this stop or take: it is filled at that price. Settling writes it into the account; anyone may.",
  lapsed: "Not filled. The order lapsed at no cost — place it again.",
});

/** The depth of the book the page draws, a side. */
export const BOOK_DEPTH = 10;

/** USDC units as a dollar amount for the screen: the library's text (`usdText`) with thousands grouped and at least two
 *  decimals. Nothing is rounded: more decimals than two stay. */
export function usdShown(units) {
  const text = usdText(units);
  const neg = text.startsWith("-");
  const [whole, frac = ""] = (neg ? text.slice(1) : text).split(".");
  return `${neg ? "-" : ""}$${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${frac.padEnd(2, "0")}`;
}

/** The marks a page built for a network that is not mainnet carries: the plaque and the tag. None on mainnet. */
export function networkMarks(config) {
  if (config?.hyperliquidChain === "Mainnet") return null;
  return { plaque: SCREEN_TEXT.testnetPlaque, tag: SCREEN_TEXT.testnetTag };
}

/** The four small steps of the strip, by where the account stands: no amounts. The exam is the step in hand while it is
 *  active, and done once passed; what comes after is the pool's, so it is not marked. */
export function lifeSteps(account) {
  return [
    { n: 1, label: "Bought", state: "done" },
    { n: 2, label: "Exam", state: account.status === "passed" ? "done" : account.status === "active" ? "now" : "" },
    { n: 3, label: "Stage", state: "" },
    { n: 4, label: "Payout", state: "" },
  ];
}

/** A moment (seconds) as "17 Nov 13:02:41", in UTC. */
export function timeText(seconds) {
  const d = new Date(Number(seconds) * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getUTCDate())} ${months[d.getUTCMonth()]} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

// ── what the page reads, as the library takes it ──────────────────

const num = (v) => (v === undefined || v === null ? v : Number(v));

/** A tuple an ethers Result holds, as a plain object by the names (and left as it is where it is one already). */
function plain(tuple, names) {
  if (tuple === null || tuple === undefined) return tuple;
  return Object.fromEntries(names.map((n, i) => [n, tuple[n] ?? tuple[i]]));
}

/**
 * `reads` for `examAccount` and `orderPreview` out of what the page asked of the chain: the exam's getters
 * (`got`, by name), and for each asset of its rules its oracle price (`oracle`), its best bid and offer (`bbo`), its
 * position and its stop and take. A price that was not read is null, and the library then says the account is
 * unpriced rather than count it at nothing.
 */
export function readsFrom(got, perAsset, touches = []) {
  const rules = plain(got.rules, ["dailyLossBps", "maxDrawdownBps", "maxLeverageX100", "assets", "assetLeverageX100"]);
  const terms = plain(got.terms, ["capital", "targetsBps", "stepDuration", "maxIdle", "minTradingDays", "costPpm", "fillDelay", "fillWindow"]);
  const pending = plain(got.pending, ["asset", "isBuy", "sz", "placedBlock"]);
  return {
    status: num(got.status), step: num(got.step), cash: got.cash, dayStartEquity: got.dayStartEquity,
    tradingDays: num(got.tradingDays), deadline: got.deadline, lastFillAt: got.lastFillAt, pending,
    filler: got.filler, fromBook: Boolean(got.fromBook), maxWorseBps: num(got.maxWorseBps),
    drawdownKind: got.drawdownKind, peakSnapshot: got.peakSnapshot, stopBook: got.stopBook ?? null,
    rules, terms,
    positions: perAsset.map((a) => ({ asset: a.asset, size: a.size, mark: a.oracle ?? null,
      stop: a.stop ?? null, take: a.take ?? null, stopChange: a.stopChange ?? null, takeChange: a.takeChange ?? null })),
    touches: touches ?? [],
  };
}

// ── words ──────────────────────────────────────────────────────

const sideWord = (isBuy) => (isBuy ? "Buy" : "Sell");

/** The perp's name and size decimals for an asset number, from the list the page read (`perps`: Map by number). */
function perpOf(perps, asset) {
  return perps.get(Number(asset)) ?? { index: Number(asset), name: `#${asset}`, szDecimals: 0, unknown: true };
}

/** An asset as the page keeps it: `hl.perp` answers a stand-in (`#<index>`, no size decimals) for an asset the list
 *  does not name, and a size or price at a scale of no decimals would be wrong by orders of magnitude. Such an asset
 *  is marked `unknown`: nothing is typed or worked out in its units. */
export function markUnknown(perp, asset) {
  const named = perp && perp.name !== `#${asset}` && Number.isInteger(Number(perp.szDecimals)) && perp.szDecimals !== null;
  return named ? perp : { ...(perp ?? {}), index: Number(asset), name: `#${asset}`, unknown: true };
}

/** Whether an asset's size decimals were read, so that a size or a price can be taken in its units. */
export const isKnown = (perp) => Boolean(perp) && perp.unknown !== true;
const DASH = "—";

/** "×" times a hundredth: a leverage kept as hundredths, said as "2.50×". For showing only. */
export function leverageText(x100) {
  if (x100 === null || x100 === undefined) return "—";
  return `${(Number(x100) / 100).toFixed(2)}×`;
}

/** The exam's account as a few labelled figures for the strip and the account panel, all said by the library's
 *  formatters. Where the account is unpriced, the figures that need a price say "Price not read" and show no old one. */
export function accountFigures(account) {
  const none = SCREEN_TEXT.noPrice;
  const usd = (v) => (v === null || v === undefined ? none : usdShown(v));
  return {
    status: account.status,
    size: usdShown(account.capital),
    step: account.target ? `${account.target.step + 1} of ${account.target.steps}` : "—",
    equity: account.priced ? usd(account.equity) : none,
    balance: usd(account.cash),
    leverage: account.priced ? leverageText(account.leverageX100) : none,
    maxLeverage: leverageText(account.maxLeverageX100),
    target: account.target ? targetFigures(account, usd, none) : null,
    lines: account.lines.map((l) => ({
      kind: l.kind, at: usd(l.at), room: account.priced ? usd(l.room) : none, near: l === account.floor,
      label: l.kind === "drawdown"
        ? (l.drawdownKind === "trailing-to-start" ? "Loss limit (follows your best day's balance up to the starting balance)" : "Total loss limit")
        : "Daily loss limit",
      from: l.kind === "daily-loss" ? `from today's snapshot ${usdShown(l.from)}` : null,
    })),
    days: account.days.needed > 0 ? `${account.days.traded} of ${account.days.needed}` : null,
    priced: account.priced,
    // Touches waiting for their settle: the figures are the account after it, and this says so, with the equity as
    // the exam counts it before the settle (every position open at its mark).
    afterSettle: afterSettleText(account.afterSettle),
  };
}

/** The target's line: how far is left (`toGo`), and once the equity is at it or past it (`reached`, the contract's
 *  `equity < targetAt` no longer true) "reached", never a negative amount to go. */
function targetFigures(account, usd, none) {
  const toGo = account.priced ? account.target.toGo : null;
  const reached = toGo !== null && toGo !== undefined && toGo <= 0n;
  return { at: usd(account.target.at), toGo: toGo === null ? none : reached ? usd(0n) : usd(toGo), reached,
    progress: toGo === null ? `to go ${none}` : reached ? "reached" : `to go ${usd(toGo)}` };
}

/** The note the header shows while touched stops or takes wait for their settle; null where none waits. */
export function afterSettleText(a) {
  if (!a) return null;
  const n = a.touches;
  const marked = a.asMarked?.equity === null || a.asMarked?.equity === undefined ? SCREEN_TEXT.noPrice : usdShown(a.asMarked.equity);
  return `After the settle of ${n} touched stop${n === 1 ? "" : "s"} or take${n === 1 ? "" : "s"}: they are filled at the `
    + `price of their touch whenever the settle comes. Before it, at the marks: equity ${marked}.`;
}

/** A position's row: name, side, size, the oracle mark, the notional, the leverage against the asset's cap, its stop and take. */
export function positionRow(p, perps) {
  const perp = perpOf(perps, p.asset);
  const dec = perp.szDecimals;
  if (perp.unknown) {
    return { asset: p.asset, name: perp.name, side: p.side === "long" ? "Long" : "Short", size: DASH, mark: DASH,
      notional: p.notional === null ? SCREEN_TEXT.noPrice : usdShown(p.notional),
      leverage: p.leverageX100 === null ? SCREEN_TEXT.noPrice : `${leverageText(p.leverageX100)} / ${leverageText(p.capX100)}`,
      stop: p.stop ? "Stop set" : null, take: p.take ? "Take set" : null, closeIsBuy: p.size < 0n, closeSize: p.size < 0n ? -p.size : p.size };
  }
  return {
    asset: p.asset, name: perp.name, side: p.side === "long" ? "Long" : "Short",
    size: `${sizeText(p.size < 0n ? -p.size : p.size, dec)} ${perp.name}`,
    mark: p.mark === null ? SCREEN_TEXT.noPrice : pxText(p.mark, dec),
    notional: p.notional === null ? SCREEN_TEXT.noPrice : usdShown(p.notional),
    leverage: p.leverageX100 === null ? SCREEN_TEXT.noPrice : `${leverageText(p.leverageX100)} / ${leverageText(p.capX100)}`,
    stop: p.stop ? triggerText(p.stop, dec) : null,
    take: p.take ? triggerText(p.take, dec) : null,
    touched: p.stop?.state === "touched" || p.take?.state === "touched",
    closeIsBuy: p.size < 0n,
    closeSize: p.size < 0n ? -p.size : p.size,
  };
}

/**
 * "Stop 950000 · closes all · armed", "Take 76000 · closes 0.01 · armed in 3 blocks", "Stop 950000 · closes all ·
 * filled at 949000 in block 1201, waiting for its settle", and a change asked: "… · moves to 960000 from block 1210" or
 * "… · taken off from block 1210" (until then this one acts).
 */
export function triggerText(t, szDecimals) {
  const name = t.kind === "take" ? "Take" : "Stop";
  const closes = t.whole ? "closes all" : `closes ${sizeText(t.closes, szDecimals)}`;
  const state = t.state === "touched" && t.touch
    ? `filled at ${pxText(t.touch.px, szDecimals)} in block ${t.touch.blk}, waiting for its settle`
    : {
      waiting: `armed in ${t.blocksToArm} block${t.blocksToArm === 1 ? "" : "s"}`,
      armed: "armed",
      reached: "reached: filled at the price of the block that logs it",
      unpriced: "price not read",
    }[t.state] ?? t.state;
  const change = !t.change ? "" : t.change.removes
    ? ` · taken off from block ${t.change.armedAt}`
    : ` · moves to ${pxText(t.change.px, szDecimals)} from block ${t.change.armedAt}`;
  return `${name} ${pxText(t.px, szDecimals)} · ${closes} · ${state}${change}`;
}

/** The waiting order's line (brief, part 5). Null where none waits. */
export function pendingText(pending, perps) {
  if (!pending) return null;
  const perp = perpOf(perps, pending.asset);
  if (pending.state === "lapsed") return { tone: "", text: SCREEN_TEXT.lapsed };
  const what = `${sideWord(pending.isBuy)} ${perp.unknown ? DASH : sizeText(pending.size, perp.szDecimals)} ${perp.name}`;
  if (pending.state === "waiting") return { tone: "", text: `Order sent: ${what} · fills from block ${pending.fillFrom}` };
  return { tone: "", text: `Being filled · window until block ${pending.fillUntil}` };
}

/** Where `value` stands between `low` and `high` as a share of that span, in hundredths of a percent, kept in 0..10000.
 *  A position on a bar only; the figures it sits between are the library's. */
export function barShare(value, low, high) {
  const span = BigInt(high) - BigInt(low);
  if (span <= 0n) return 0;
  const at = ((BigInt(value) - BigInt(low)) * 10_000n) / span;
  return Number(at < 0n ? 0n : at > 10_000n ? 10_000n : at);
}

/**
 * The scale from the lowest loss line to the target: where each line, the equity and the target stand on it. Null
 * where the account is unpriced or the exam has no target, since a bar needs both its ends and the equity.
 */
export function accountScale(account) {
  if (!account.priced || !account.target || !account.lines.length) return null;
  const low = account.lines.reduce((m, l) => (l.at < m ? l.at : m), account.lines[0].at);
  let high = account.target.at;
  if (account.equity > high) high = account.equity;
  const mark = (value) => barShare(value, low, high);
  return {
    equity: mark(account.equity),
    target: mark(account.target.at),
    lines: account.lines.map((l) => ({ kind: l.kind, share: mark(l.at), near: l === account.floor })),
    broken: account.breach !== null && account.breach !== "none",
  };
}

// ── the order form ──────────────────────────────────────────────

/**
 * An order as the form has it: the text typed, turned into size units by the library, then worked out by
 * `orderPreview`. A size that cannot be read is said in the library's words (`error`) and nothing else is worked out.
 * `prices` are the asset's, in the contract's price units: `{oracle, bid, ask}`, null where not read.
 */
export function orderForm(reads, { asset, perp, isBuy, sizeInput, prices, now, block }) {
  if (!isKnown(perp)) return { size: null, error: SCREEN_TEXT.noAsset, preview: null };
  const text = String(sizeInput ?? "").trim();
  if (text === "") return { size: null, error: null, preview: null };
  let size;
  try {
    size = sizeUnits(text, perp.szDecimals);
  } catch (err) {
    return { size: null, error: err.message, preview: null };
  }
  const preview = orderPreview(reads, { asset, isBuy, size, oracle: prices.oracle, bid: prices.bid, ask: prices.ask, now, block });
  return { size, error: null, preview };
}

/** The estimate's rows and warnings, as the page shows them (brief, part 4). */
export function previewView(preview, perp) {
  if (!preview) return null;
  if (preview.refusal !== null) return { blocked: true, rows: [], warnings: [{ tone: "bad", text: preview.text }], note: preview.note };
  const dec = perp.szDecimals;
  const warnings = [];
  if (preview.atFill === "OverLeverage") warnings.push({ tone: "bad", text: preview.atFillText });
  else if (preview.atFill === "TooFarFromOracle") warnings.push({ tone: "warn", text: preview.atFillText });
  if (preview.after.breach !== null && preview.after.breach !== "none") warnings.push({ tone: "bad", text: SCREEN_TEXT.breach });
  if (preview.closesPosition) warnings.push({ tone: "", text: SCREEN_TEXT.closeDrops
    + (preview.dropsTriggers.length ? ` This one takes off the ${preview.dropsTriggers.join(" and the ")}.` : "") });
  return {
    blocked: false,
    rows: [
      ["Est. fill price", `${pxText(preview.px, dec)} (${preview.source === "book" ? "book" : "oracle"})`],
      ["Cost", usdShown(preview.cost)],
      ["Position after", `${sizeText(preview.after.position, dec)} ${perp.name}`],
      ["Leverage after", preview.after.leverageX100 === null ? SCREEN_TEXT.noPrice : leverageText(preview.after.leverageX100)],
    ],
    warnings, note: preview.note,
  };
}

/** A stop or a take being set: the text typed turned into a price by the library, then `triggerSetPreview`. */
export function triggerForm(reads, { asset, perp, isTake, pxInput, sizeInput, oracle, now, block }) {
  if (!isKnown(perp)) return { px: null, size: 0n, error: SCREEN_TEXT.noAsset, preview: null };
  const priceText = String(pxInput ?? "").trim();
  if (priceText === "") return { px: null, size: 0n, error: null, preview: null };
  let px;
  let size = 0n;
  try {
    px = pxUnits(priceText, perp.szDecimals);
    if (String(sizeInput ?? "").trim() !== "") size = sizeUnits(sizeInput, perp.szDecimals);
  } catch (err) {
    return { px: null, size: 0n, error: err.message, preview: null };
  }
  return { px, size, error: null, preview: triggerSetPreview(reads, { asset, isTake, px, size, oracle, now, block }) };
}

/** The words under the "Set stop" / "Set take" button. */
export function triggerPreviewView(preview, perp) {
  if (!preview) return null;
  if (preview.refusal !== null) return { blocked: true, lines: [{ tone: "bad", text: preview.text }] };
  const lines = [{ tone: "", text: `${preview.kind === "take" ? "Take" : "Stop"} ${pxText(preview.px, perp.szDecimals)} · closes `
    + `${preview.whole ? "all" : sizeText(preview.closes, perp.szDecimals)} · acts from block ${preview.armedAt}` }];
  if (preview.replaces && preview.isChange) {
    lines.push({ tone: "", text: `Moves the ${preview.kind} at ${pxText(preview.replaces.px, perp.szDecimals)}: that one acts `
      + `until block ${preview.armedAt}, and is filled if the price touches it before then.` });
  } else if (preview.replaces) {
    lines.push({ tone: "", text: `Replaces the ${preview.kind} at ${pxText(preview.replaces.px, perp.szDecimals)}.` });
  }
  if (preview.reached) lines.push({ tone: "warn", text: SCREEN_TEXT.triggerReached });
  return { blocked: false, lines };
}

// ── the book ────────────────────────────────────────────────────

/** The book as the page draws it: asks above, bids below, each side's depth as a share of the larger side's. */
export function bookView(raw, depth = BOOK_DEPTH) {
  const b = bookSides(raw, depth);
  const most = Math.max(b.bids.at(-1)?.total ?? 0, b.asks.at(-1)?.total ?? 0);
  const share = (l) => (most > 0 ? Math.round((l.total / most) * 100) : 0);
  return {
    asks: [...b.asks].reverse().map((l) => ({ ...l, share: share(l) })),
    bids: b.bids.map((l) => ({ ...l, share: share(l) })),
    spread: b.spreadBps === null ? "—" : `${b.spreadBps.toFixed(1)} bps`,
    spreadAbs: b.bestBid === null || b.bestAsk === null ? "—" : String(Number((b.bestAsk - b.bestBid).toFixed(6))),
    mid: b.mid, empty: b.bids.length === 0 && b.asks.length === 0,
  };
}

// ── the journal ─────────────────────────────────────────────────

const SOURCE = ["oracle", "book", "oracle (book was off)"];

/**
 * The journal's rows out of decoded events ({name, args, block}), oldest first: each with a line of words (`text`) and
 * the cells of the table (`cells`: event, asset, side, size, price, cost, from, note). A stop or a take that was
 * carried out writes its `Filled` first and its `TriggerFilled` right after: that one row reads "Stop filled".
 */
export function journalRows(events, perps) {
  const rows = [];
  let lastFilled = null;
  const dash = "—";
  for (const e of events) {
    const a = e.args;
    const perp = perpOf(perps, a.asset ?? 0);
    const dec = perp.szDecimals;
    const S = (n) => (perp.unknown ? DASH : sizeText(n, dec));
    const P = (n) => (perp.unknown ? DASH : pxText(n, dec));
    const at = { block: e.block, name: e.name };
    const cells = (c) => ({ event: dash, asset: perp.name, side: dash, size: dash, price: dash, cost: dash, from: dash, note: dash, ...c });
    if (e.name === "Placed") {
      rows.push({ ...at, text: `Order placed: ${sideWord(a.isBuy)} ${S(a.sz)} ${perp.name} · fills from block ${a.fillFrom} to ${a.fillUntil}`,
        cells: cells({ event: "Placed", side: sideWord(a.isBuy), size: S(a.sz), note: `blocks ${a.fillFrom} to ${a.fillUntil}` }) });
    } else if (e.name === "Filled") {
      const late = Number(a.late);
      const source = SOURCE[Number(a.source)] ?? "oracle";
      const head = `${sideWord(a.isBuy)} ${S(a.sz)} ${perp.name} at ${P(a.px)} · cost ${usdShown(a.cost)} · price from the ${source}`;
      lastFilled = { ...at, head, text: `Filled: ${head} · ${late === 0 ? "first block it could be filled" : `${late} block${late === 1 ? "" : "s"} after the first`}`,
        cells: cells({ event: "Filled", side: sideWord(a.isBuy), size: S(a.sz), price: P(a.px), cost: usdShown(a.cost),
          from: source, note: `${late} block${late === 1 ? "" : "s"}` }) };
      rows.push(lastFilled);
    } else if (e.name === "TriggerFilled") {
      if (lastFilled) {
        // With a stop book the close counts at its touch: the oracle reached the level in that block.
        const touch = a.touchBlock === undefined || a.touchBlock === null ? ""
          : ` · touched in block ${a.touchBlock} at ${P(a.touchPx)}`;
        lastFilled.text = `${a.isTake ? "Take" : "Stop"} filled: ${lastFilled.head} · it was set at ${P(a.triggerPx)}${touch}`;
        lastFilled.cells.event = a.isTake ? "Take filled" : "Stop filled";
        lastFilled.cells.note = `set at ${P(a.triggerPx)}${touch}`;
      }
    } else if (e.name === "Lapsed") {
      rows.push({ ...at, text: `Order lapsed at no cost: ${sideWord(a.isBuy)} ${S(a.sz)} ${perp.name}`,
        cells: cells({ event: "Lapsed", side: sideWord(a.isBuy), size: S(a.sz) }) });
    } else if (e.name === "DaySnapshot") {
      rows.push({ ...at, text: `Day ${a.day} snapshot: equity ${usdShown(a.equity)}`,
        cells: cells({ event: "DaySnapshot", asset: dash, note: `equity ${usdShown(a.equity)}` }) });
    } else if (e.name === "TriggerSet") {
      const closes = BigInt(a.sz) === 0n ? "all" : S(a.sz);
      if (BigInt(a.px) === 0n) {
        // With a stop book, taking one off is a change too: it acts until that block.
        rows.push({ ...at, text: `${a.isTake ? "Take" : "Stop"} on ${perp.name} taken off from block ${a.armedAt}`,
          cells: cells({ event: a.isTake ? "Take off" : "Stop off", note: `from block ${a.armedAt}` }) });
      } else {
        rows.push({ ...at, text: `${a.isTake ? "Take" : "Stop"} set on ${perp.name} at ${P(a.px)} · closes ${closes} · acts from block ${a.armedAt}`,
          cells: cells({ event: a.isTake ? "Take set" : "Stop set", size: closes, price: P(a.px), note: `acts from block ${a.armedAt}` }) });
      }
    } else if (e.name === "TriggerCleared") {
      rows.push({ ...at, text: `${a.isTake ? "Take" : "Stop"} on ${perp.name} removed`, cells: cells({ event: a.isTake ? "Take removed" : "Stop removed" }) });
    }
    if (e.name !== "Filled") lastFilled = null;
  }
  return rows;
}

/** One call in flight at a time: `run(fn)` does nothing while a call is out, and `busy` is true from its start to its end. */
export function singleFlight() {
  let busy = false;
  return {
    get busy() { return busy; },
    async run(fn) {
      if (busy) return { skipped: true };
      busy = true;
      try {
        return { skipped: false, value: await fn() };
      } finally {
        busy = false;
      }
    },
  };
}

/** The blocks one press of a journal button reads. */
export const JOURNAL_BLOCKS = 1000;

/**
 * The next range of blocks to read, given what is loaded (`loaded`: {from, top}, or null for nothing yet) and the
 * latest block: the newest blocks at first, then older ones, or the ones since the top of what is loaded. Null where
 * there is nothing more in that direction. The ranges join: what is loaded is always one run of blocks.
 */
export function journalPlan({ latest, loaded, direction }) {
  if (!loaded) return { from: Math.max(0, latest - JOURNAL_BLOCKS + 1), to: latest };
  if (direction === "newer") {
    return loaded.top >= latest ? null : { from: loaded.top + 1, to: Math.min(latest, loaded.top + JOURNAL_BLOCKS) };
  }
  return loaded.from <= 0 ? null : { from: Math.max(0, loaded.from - JOURNAL_BLOCKS), to: loaded.from - 1 };
}

/** Whether there is anything before the blocks loaded: false once the range starts at block 0, and the control that loads
 *  earlier events is then off. Nothing loaded yet: the first load is open. */
export const canLoadEarlier = (loaded) => !loaded || loaded.from > 0;

/** What the journal says of itself: only the blocks that were read. */
export function journalRange(loaded) {
  return loaded ? `Events from block ${loaded.from} to block ${loaded.top}.` : "";
}

/** What is left until `endsAt` (seconds), as "2 d 3 h", "5 h 12 min" or "7 min"; "Over" at or past it, "—" with no end. */
export function timeLeft(endsAt, now) {
  if (endsAt === null || endsAt === undefined) return "—";
  const left = Number(endsAt) - Number(now);
  if (left <= 0) return "Over";
  const d = Math.floor(left / 86_400);
  const h = Math.floor((left % 86_400) / 3600);
  const m = Math.floor((left % 3600) / 60);
  if (d > 0) return `${d} d ${h} h`;
  if (h > 0) return `${h} h ${m} min`;
  return `${Math.max(1, m)} min`;
}

/** A refusal of a sent call, in the contract's plain words where its revert data is one of its own errors. */
export function sendFailure(err, refusalOfData) {
  const data = [err?.data, err?.info?.error?.data, err?.error?.data].find((d) => typeof d === "string" && d.startsWith("0x"));
  const known = data ? refusalOfData(data) : null;
  if (known) return known.text;
  if (err?.code === "ACTION_REJECTED" || err?.code === 4001) return "Cancelled in the wallet.";
  return String(err?.shortMessage || err?.message || err || "The call failed.").slice(0, 300);
}

export { refusalText };

// ── the market list and the unit of the size ───────────────────────────────────────────────────────────

/** The asset the page's address names (`?asset=<number>`), when the exam trades it; else the exam's first. */
export function assetFromQuery(query, assets) {
  const wanted = new URLSearchParams(String(query ?? "")).get("asset");
  const n = wanted !== null && /^\d+$/.test(wanted) ? Number(wanted) : null;
  return n !== null && assets.includes(n) ? n : assets[0];
}

/** The address's query with `asset` set, whatever else it carries kept: what a link to the same market holds. */
export function queryWithAsset(query, asset) {
  const q = new URLSearchParams(String(query ?? ""));
  q.set("asset", String(asset));
  return q.toString();
}

/**
 * The rows of the market list: only the exam's own assets (`assets`, from its `rules()`), each with its name, the
 * oracle price, the leverage cap of that asset (`rules.assetLeverageX100`, else the account's) and whether a position
 * is open on it; `query` keeps the names that contain it, in any case. `perAsset` is what the page read of each asset.
 */
export function marketRows({ assets, perps, perAsset, rules, query = "" }) {
  const q = String(query).trim().toLowerCase();
  return assets.map((asset, i) => {
    const perp = perps.get(asset);
    const read = perAsset.find((a) => a.asset === asset);
    const cap = rules?.assetLeverageX100?.[i] ?? rules?.maxLeverageX100;
    return {
      asset, name: perp?.name ?? `#${asset}`,
      price: read && read.oracle != null && isKnown(perp) ? pxText(read.oracle, perp.szDecimals) : DASH,
      leverage: cap === undefined || cap === null ? DASH : `${Number(cap) / 100}×`,
      held: Boolean(read) && BigInt(read.size ?? 0) !== 0n,
    };
  }).filter((r) => q === "" || r.name.toLowerCase().includes(q));
}

/** The highlighted row after a key in the list of `count` rows: the arrows step (and wrap), Home and End jump; -1 for none. */
export function stepIndex(count, index, key) {
  if (count <= 0) return -1;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  if (key === "ArrowDown") return index < 0 ? 0 : (index + 1) % count;
  if (key === "ArrowUp") return index < 0 ? count - 1 : (index - 1 + count) % count;
  return Math.min(Math.max(index, -1), count - 1);
}

/** The units a size can be typed in: the asset's own, or USDC. What the browser remembers is one of these two words. */
export const SIZE_UNITS = Object.freeze(["asset", "usdc"]);
export const sizeUnitOf = (stored) => (SIZE_UNITS.includes(stored) ? stored : "asset");

/** An amount of USDC typed ("1035.03") in the contract's units, exactly; more than six decimals are refused. */
export function usdcUnits(text) {
  const m = /^(\d+)(?:\.(\d+))?$/.exec(String(text).trim());
  if (!m) throw new Error(`The amount is not a plain positive number: ${text}`);
  const frac = m[2] ?? "";
  if (frac.length > 6 && /[1-9]/.test(frac.slice(6))) throw new Error("An amount has at most 6 decimals.");
  return BigInt(m[1]) * 1_000_000n + BigInt((frac.slice(0, 6) || "0").padEnd(6, "0"));
}

const fixedSize = (size, perp) => {
  const [whole, frac = ""] = sizeText(size, perp.szDecimals).split(".");
  return perp.szDecimals > 0 ? `${whole}.${frac.padEnd(perp.szDecimals, "0")}` : whole;
};

/**
 * The size field as the trader typed it, in the asset's units or in USDC, as the asset-unit text the order takes.
 * In USDC the size is the amount divided at the price the estimate uses for this side (`fillPrice`, the same as "Est.
 * fill price") and rounded down (`sizeFromUsdc`); the field is off, with the reason, while that price is not read. In the
 * asset's units the size is as typed and the amount is shown under it. `conv` is the other number, always with a "≈".
 * Nothing is worked out here but what the library works out.
 */
export function sizeEntry(reads, { perp, isBuy, unit, input, prices }) {
  const text = String(input ?? "").trim();
  const have = prices.oracle !== null && prices.oracle !== undefined && BigInt(prices.oracle) > 0n;
  const fill = have ? fillPrice({ isBuy, oracle: prices.oracle, bid: prices.bid, ask: prices.ask, fromBook: Boolean(reads.fromBook), maxWorseBps: reads.maxWorseBps }) : null;
  const none = { sizeInput: "", error: null, disabled: null, conv: "" };
  if (!isKnown(perp)) return none;
  if (unit === "usdc") {
    if (!fill) return { ...none, disabled: SCREEN_TEXT.usdcNoPrice };
    if (text === "") return none;
    try {
      const size = sizeFromUsdc(usdcUnits(text), fill.px);
      return { ...none, sizeInput: sizeText(size, perp.szDecimals), conv: `≈ ${fixedSize(size, perp)} ${perp.name}` };
    } catch (err) {
      return { ...none, error: err.message };
    }
  }
  if (text === "") return { ...none, sizeInput: "" };
  let size = null;
  try {
    size = sizeUnits(text, perp.szDecimals);
  } catch {
    size = null;
  }
  return { ...none, sizeInput: text, conv: size !== null && fill ? `≈ ${usdShown(size * fill.px)}` : "" };
}
